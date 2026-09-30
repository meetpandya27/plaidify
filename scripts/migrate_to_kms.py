"""Move every encrypted artifact from one KMS provider to another.

Zero-downtime migration helper for issue #26.

Usage (from project root, .venv activated):

    # Re-wrap from current local master key to AWS KMS
    KMS_PROVIDER=local SOURCE_KMS_PROVIDER=local TARGET_KMS_PROVIDER=aws \\
        KMS_KEY_ID=arn:aws:kms:us-east-1:123:key/abc \\
        python -m scripts.migrate_to_kms

What moves:
  1. ``users.encrypted_dek`` — every per-user DEK is unwrapped with the source
     provider and re-wrapped with the target. A user without a DEK gets one,
     wrapped by the target, so their data can move under it too.
  2. ``access_tokens`` credentials still encrypted directly under
     ENCRYPTION_KEY (rows from before per-user DEKs) are re-encrypted under
     the owner's DEK.
  3. ``webhooks.secret`` values under ENCRYPTION_KEY move under the owner's
     DEK. Readers must use ``database.decrypt_webhook_secret``, which accepts
     both forms.
  Everything else that is encrypted at rest (credentials and access-job
  results under a DEK) follows its DEK and needs no rewrite.

Not moved: access-job dispatch payloads live in Redis, encrypted under
ENCRYPTION_KEY, and expire after ACCESS_JOB_PAYLOAD_TTL. ENCRYPTION_KEY stays
configured after the migration (it still decrypts pre-migration leftovers).

Each row is written with a conditional UPDATE and committed in batches. Rows
already under the target are recognised and left alone, so the script can be
re-run after a partial failure. Owned rows are walked owner by owner, so only
one user's DEK is held in memory at a time (it is never written or logged).

Exit status: 0 when everything was migrated; 1 when any row was skipped or
failed (see the log, fix, re-run); 2 on a configuration error.

Roll-forward strategy:
  - Run the migration with the application offline OR while writes are
    paused (kms_provider still pointing to source).
  - Flip ``KMS_PROVIDER`` to the target value.
  - Restart the app. New writes go to the target; existing reads succeed
    because the rows now contain target-wrapped envelopes.

Roll-back:
  - Re-run with SOURCE / TARGET reversed before restarting the app.
"""

from __future__ import annotations

import logging
import os
import sys
from collections import Counter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("migrate_to_kms")

_BATCH = 200


def _migrate_deks(db, src_provider, tgt_provider, target_is_local: bool, counts: Counter) -> None:
    """Re-wrap every user's DEK under the target (creating one where missing)."""
    from sqlalchemy import select, update

    from src.database import User, get_current_key_version

    stamp = get_current_key_version() if target_is_local else None
    last_id = 0
    while True:
        rows = db.execute(
            select(User.id, User.encrypted_dek).where(User.id > last_id).order_by(User.id).limit(_BATCH)
        ).all()
        if not rows:
            return
        for user_id, wrapped in rows:
            last_id = user_id
            if wrapped is None:
                condition = User.encrypted_dek.is_(None)
                new_wrapped = tgt_provider.wrap_key_sync(os.urandom(32))
                outcome = "deks_created"
            else:
                try:
                    dek = src_provider.unwrap_key_sync(wrapped)
                except Exception as exc:
                    try:
                        tgt_provider.unwrap_key_sync(wrapped)
                        counts["deks_already_migrated"] += 1
                    except Exception:
                        logger.warning("user %s: DEK unwraps with neither provider (%s) -- skipping", user_id, exc)
                        counts["skipped"] += 1
                    continue
                try:
                    new_wrapped = tgt_provider.wrap_key_sync(dek)
                except Exception as exc:
                    logger.error("user %s: target wrap failed: %s", user_id, exc)
                    counts["failed"] += 1
                    continue
                condition = User.encrypted_dek == wrapped
                outcome = "deks_rewrapped"
            result = db.execute(
                update(User)
                .where(User.id == user_id, condition)
                .values(encrypted_dek=new_wrapped, dek_key_version=stamp)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount:
                counts[outcome] += 1
                logger.info("user %s: %s", user_id, outcome.replace("deks_", "DEK "))
            else:
                logger.warning("user %s: DEK changed concurrently -- skipping, re-run to migrate it", user_id)
                counts["skipped"] += 1
        db.commit()


def _under_dek(dek: bytes, ciphertext: str) -> str | None:
    """``ciphertext`` moved under ``dek``, or None if it already is."""
    from src.database import decrypt_credential, decrypt_with_dek, encrypt_with_dek

    try:
        decrypt_with_dek(dek, ciphertext)
        return None
    except Exception:
        pass
    return encrypt_with_dek(dek, decrypt_credential(ciphertext))


class _OwnerDEK:
    """The DEK of the owner currently being processed (unwrapped with the target provider)."""

    def __init__(self, db, tgt_provider) -> None:
        self._db = db
        self._provider = tgt_provider
        self._user_id = None
        self._dek: bytes | None = None

    def get(self, user_id: int) -> bytes | None:
        from sqlalchemy import select

        from src.database import User

        if user_id != self._user_id:
            self._user_id, self._dek = user_id, None
            wrapped = self._db.execute(select(User.encrypted_dek).where(User.id == user_id)).scalar()
            if wrapped:
                try:
                    self._dek = self._provider.unwrap_key_sync(wrapped)
                except Exception as exc:
                    logger.warning("user %s: DEK does not unwrap with the target provider (%s)", user_id, exc)
        return self._dek


def _migrate_owned_rows(db, tgt_provider, model, key_column, fields: tuple[str, ...], label, counts: Counter) -> None:
    """Move the master-key ``fields`` of every ``model`` row under its owner's DEK."""
    from sqlalchemy import select, tuple_, update

    from src.database import get_current_key_version

    version = get_current_key_version()
    owner_dek = _OwnerDEK(db, tgt_provider)
    columns = [getattr(model, name) for name in fields]
    after = None
    while True:
        query = select(model.user_id, key_column, *columns).order_by(model.user_id, key_column).limit(_BATCH)
        if after is not None:
            query = query.where(tuple_(model.user_id, key_column) > tuple_(*after))
        rows = db.execute(query).all()
        if not rows:
            return
        for user_id, key, *ciphertexts in rows:
            after = (user_id, key)
            dek = owner_dek.get(user_id)
            if dek is None:
                logger.warning("%s: owner's DEK is unavailable -- skipping", label(key))
                counts["skipped"] += 1
                continue
            try:
                moved = [_under_dek(dek, ciphertext) for ciphertext in ciphertexts]
            except Exception as exc:
                logger.warning("%s: does not decrypt (%s) -- skipping", label(key), exc)
                counts["skipped"] += 1
                continue
            if all(value is None for value in moved):
                continue
            values = {name: new or old for name, new, old in zip(fields, moved, ciphertexts, strict=True)}
            result = db.execute(
                update(model)
                .where(key_column == key, *(column == old for column, old in zip(columns, ciphertexts, strict=True)))
                .values(**values, key_version=version)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount:
                counts[f"{model.__tablename__}_moved"] += 1
            else:
                logger.warning("%s: changed concurrently -- skipping, re-run to migrate it", label(key))
                counts["skipped"] += 1
        db.commit()


def main() -> int:
    source = (os.environ.get("SOURCE_KMS_PROVIDER") or os.environ.get("KMS_PROVIDER") or "local").lower()
    target = (os.environ.get("TARGET_KMS_PROVIDER") or "").lower()
    if not target:
        logger.error("TARGET_KMS_PROVIDER environment variable is required.")
        return 2
    if source == target:
        logger.error("SOURCE and TARGET providers are identical (%s); nothing to do.", source)
        return 2

    # Defer imports so logging is configured before SQLAlchemy noises begin.
    from src.crypto import token_fingerprint
    from src.database import AccessToken, SessionLocal, Webhook
    from src.kms import _PROVIDERS, reset_kms_provider

    if source not in _PROVIDERS:
        logger.error("Unknown source provider %r. Known: %s", source, list(_PROVIDERS))
        return 2
    if target not in _PROVIDERS:
        logger.error("Unknown target provider %r. Known: %s", target, list(_PROVIDERS))
        return 2

    try:
        src_provider = _PROVIDERS[source]()
        tgt_provider = _PROVIDERS[target]()
    except Exception as exc:
        logger.error("Could not configure the providers: %s", exc)
        return 2

    counts: Counter = Counter()
    db = SessionLocal()
    try:
        logger.info("Migrating encrypted data: %s -> %s", source, target)
        _migrate_deks(db, src_provider, tgt_provider, target == "local", counts)
        _migrate_owned_rows(
            db,
            tgt_provider,
            AccessToken,
            AccessToken.token,
            ("username_encrypted", "password_encrypted"),
            lambda token: f"access token {token_fingerprint(token)}",
            counts,
        )
        _migrate_owned_rows(
            db, tgt_provider, Webhook, Webhook.id, ("secret",), lambda webhook_id: f"webhook {webhook_id}", counts
        )
    except Exception:
        db.rollback()
        logger.exception("Migration aborted; committed batches are kept and a re-run resumes")
        counts["failed"] += 1
    finally:
        db.close()

    logger.info(
        "Migration finished: deks_rewrapped=%d deks_created=%d deks_already_migrated=%d credentials_moved=%d "
        "webhook_secrets_moved=%d skipped=%d failed=%d",
        counts["deks_rewrapped"],
        counts["deks_created"],
        counts["deks_already_migrated"],
        counts["access_tokens_moved"],
        counts["webhooks_moved"],
        counts["skipped"],
        counts["failed"],
    )
    # Reset the cached singleton so subsequent in-process callers re-resolve.
    reset_kms_provider()
    if counts["skipped"] or counts["failed"]:
        logger.error("Migration INCOMPLETE: do not switch KMS_PROVIDER until a re-run finishes cleanly.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
