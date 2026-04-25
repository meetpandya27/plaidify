"""Re-wrap every per-user DEK from one KMS provider to another.

Zero-downtime migration helper for issue #26.

Usage (from project root, .venv activated):

    # Re-wrap from current local master key to AWS KMS
    KMS_PROVIDER=local SOURCE_KMS_PROVIDER=local TARGET_KMS_PROVIDER=aws \\
        KMS_KEY_ID=arn:aws:kms:us-east-1:123:key/abc \\
        python -m scripts.migrate_to_kms

The script:
  1. Resolves SOURCE_KMS_PROVIDER (defaults to current ``KMS_PROVIDER``)
     and TARGET_KMS_PROVIDER from the environment.
  2. Iterates every ``users.encrypted_dek`` row.
  3. Unwraps with the source provider, re-wraps with the target.
  4. Writes the new envelope back inside a single transaction per user.

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("migrate_to_kms")


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
    from src.database import User, get_db
    from src.kms import _PROVIDERS, reset_kms_provider

    if source not in _PROVIDERS:
        logger.error("Unknown source provider %r. Known: %s", source, list(_PROVIDERS))
        return 2
    if target not in _PROVIDERS:
        logger.error("Unknown target provider %r. Known: %s", target, list(_PROVIDERS))
        return 2

    src_provider = _PROVIDERS[source]()
    tgt_provider = _PROVIDERS[target]()

    db = next(get_db())
    rewrapped = 0
    skipped = 0
    failed = 0
    try:
        users = db.query(User).filter(User.encrypted_dek.isnot(None)).all()
        logger.info("Re-wrapping %d user DEK(s): %s -> %s", len(users), source, target)
        for user in users:
            try:
                dek = src_provider.unwrap_key_sync(user.encrypted_dek)
            except Exception as exc:
                logger.warning("user %s: source unwrap failed (%s) -- skipping", user.id, exc)
                skipped += 1
                continue
            try:
                user.encrypted_dek = tgt_provider.wrap_key_sync(dek)
                db.commit()
                rewrapped += 1
                logger.info("user %s: re-wrapped", user.id)
            except Exception as exc:
                db.rollback()
                logger.error("user %s: target wrap failed: %s", user.id, exc)
                failed += 1
    finally:
        db.close()

    logger.info(
        "Migration finished: re-wrapped=%d skipped=%d failed=%d", rewrapped, skipped, failed,
    )
    # Reset the cached singleton so subsequent in-process callers re-resolve.
    reset_kms_provider()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
