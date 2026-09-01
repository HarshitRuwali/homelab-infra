#!/usr/bin/env bash
#
# REMOVED. Replaced by a single command that does bucket + secret + IAM setup
# in the right order and writes the results into /etc/s3-backup/backup.env.
#
# This file exists only to say so, because "command not found" does not.
#
echo "aws/secret-setup.sh was removed." >&2
echo >&2
echo "Use instead:" >&2
echo "    sudo s3-backup-setup-aws --bucket YOUR-BUCKET --region YOUR-REGION" >&2
echo "    sudo s3-backup-setup-aws --bucket YOUR-BUCKET --region YOUR-REGION --apply" >&2
echo >&2
echo "It creates the bucket, both IAM users, the Secrets Manager secret and the" >&2
echo "access keys, and writes them into the config itself. Dry run by default." >&2
echo "See docs/setup.md." >&2
exit 1
