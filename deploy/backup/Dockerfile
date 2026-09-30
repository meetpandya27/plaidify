# Scheduled, encrypted PostgreSQL backups: scripts/backup_db.sh with the
# PostgreSQL 16 client tools and age.
#
# Built from the repository root (the root .dockerignore allow-lists the
# script) by docker-compose.production.yml's `backup` service; the Kubernetes
# CronJob in deploy/backup-cronjob.yaml runs the same image.
#
#   docker build -f deploy/backup/Dockerfile -t plaidify-backup .

FROM postgres:16-alpine

RUN apk add --no-cache age bash tini \
    && mkdir -p /backups \
    && chown postgres:postgres /backups \
    && chmod 700 /backups

COPY --chmod=755 scripts/backup_db.sh /usr/local/bin/backup_db.sh

# The image's own server never starts; the client tools run unprivileged.
USER postgres
ENV BACKUP_DIR=/backups

ENTRYPOINT ["/sbin/tini", "--", "backup_db.sh"]
CMD ["schedule"]
