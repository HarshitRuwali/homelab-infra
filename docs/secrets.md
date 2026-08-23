# Secrets

Two backends, selected by `SECRETS_BACKEND` in `/etc/s3-backup/backup.env`.

| | `file` | `aws-secrets-manager` |
|---|---|---|
| restic password | `/etc/s3-backup/restic-password`, permanently on disk | Secrets Manager; written to tmpfs for the length of a run |
| S3 access key | `backup.env`, permanently on disk | Secrets Manager (optional) |
| On disk permanently | everything | only the bootstrap credential |

## Why a bootstrap credential still exists

Reading a secret from AWS requires an AWS credential, so that one credential
cannot itself be fetched. `aws/secret-setup.sh` creates an IAM user whose only
permission is `GetSecretValue` on one secret ARN, with an explicit `Deny` on
every other Secrets Manager action.

**Be clear about what this buys.** Anyone who obtains the bootstrap key can
read the secret, so a stolen disk is still a full compromise in one hop. What
you gain is:

- **Central revocation.** Server stolen or decommissioned? Disable one IAM user
  and every copy of the credential is dead, without touching any host.
- **Central rotation.** Rotating the S3 key means updating the secret, not
  editing files on each machine.
- **The restic password is no longer stored beside the data it protects.** It
  is not on the HDD, not in `backup.env`, and not in a config backup.
- **Nothing sensitive is at rest after a run.** The password lives on tmpfs
  and is removed when the run ends; systemd's `RuntimeDirectory=` deletes
  `/run/s3-backup` even if the process is `SIGKILL`ed.

If you want no long-lived key on disk at all, that is **IAM Roles Anywhere**:
set `BOOTSTRAP_AWS_PROFILE` to a profile in `/root/.aws` that uses
`credential_process`. In that configuration the secret must also carry the S3
keys, because the runner image has no credential helper — `s3-backup` fails
with an explicit message if it does not.

## The trade-off you are accepting

> Putting the restic password in the same AWS account as the restic repository
> means one account compromise yields both the ciphertext and the key. With
> `SECRETS_BACKEND=file`, an attacker with AWS access gets an encrypted repo
> they cannot read.

Two consequences worth acting on:

1. **Keep an offline copy of the restic password anyway.** Password manager,
   printout, anywhere outside AWS. If you lose access to the AWS account you
   lose the backups *and* the key to them. The offline copy is what makes that
   recoverable if you ever manage to get the objects back another way.
2. **Consider a customer-managed KMS key.** Pass `--kms-key-id` to
   `secret-setup.sh` and give the key a restrictive key policy. An IAM
   principal then needs both `GetSecretValue` *and* `kms:Decrypt` to read the
   password, which a broad but not unlimited compromise may not have.

If neither is acceptable for your threat model, stay on `SECRETS_BACKEND=file`
and keep the password off-box manually. That is a legitimate choice.

## Setting it up

```bash
# New installation - generate a password that only ever exists in AWS
aws/secret-setup.sh --secret-id homelab/s3-backup --region ap-south-1 --generate
aws/secret-setup.sh --secret-id homelab/s3-backup --region ap-south-1 --generate --apply

# Then create the bootstrap key (not done by the script, so it never lands in
# a log or your shell history)
aws iam create-access-key --user-name s3-backup-bootstrap
```

Put the result in `backup.env`:

```bash
SECRETS_BACKEND="aws-secrets-manager"
SECRET_ID="homelab/s3-backup"
BOOTSTRAP_AWS_ACCESS_KEY_ID="AKIA..."
BOOTSTRAP_AWS_SECRET_ACCESS_KEY="..."
```

### Secret format

Either a JSON object:

```json
{
  "restic_password": "...",
  "aws_access_key_id": "AKIA...",
  "aws_secret_access_key": "..."
}
```

...or a bare string, which is taken to be the restic password on its own (the
S3 keys then stay in `backup.env`). The field names are configurable via
`SECRET_KEY_*`. Only string values are accepted; a number or `null` is treated
as absent, and a secret that is not a JSON object is a hard error rather than a
silent empty password.

## Migrating from `file`

```bash
# 1. Move the existing password into the secret, unchanged
sudo aws/secret-setup.sh --secret-id homelab/s3-backup --region ap-south-1 \
     --restic-password-file /etc/s3-backup/restic-password --apply

# 2. Switch backends in /etc/s3-backup/backup.env, add the bootstrap key

# 3. Prove the repository still opens BEFORE deleting anything
sudo s3-backup preflight
sudo s3-backup snapshots

# 4. Only now remove the on-disk copy
sudo shred -u /etc/s3-backup/restic-password
```

Step 3 is the whole point of the ordering: if the secret is wrong, you still
have the file.

## Rotation

**S3 access key** — routine. Create the new key, update the secret with
`secret-setup.sh --apply`, run `sudo s3-backup preflight`, delete the old key.
No server is touched.

**Bootstrap key** — create the new key, update `backup.env` on each host, run
`preflight`, delete the old key.

**restic password** — *not* a matter of editing the secret. Changing the stored
value only makes the repository unopenable. Use restic's own key management:

```bash
# add a new key to the repository, then remove the old one
docker run --rm -it -e RESTIC_REPOSITORY -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
  -e AWS_DEFAULT_REGION -e RESTIC_PASSWORD_FILE=/run/secrets/pw \
  -v /run/s3-backup/restic-password:/run/secrets/pw:ro \
  s3-backup-runner:1.0.0 restic key add
```

Then update the secret to the new password, verify with `s3-backup snapshots`,
and only then `restic key remove` the old one.

## Disaster recovery

During a full rebuild you need the restic password *before* `s3-backup` can
run. Get it directly:

```bash
aws secretsmanager get-secret-value --secret-id homelab/s3-backup \
  --query SecretString --output text | jq -r .restic_password
```

That command needs a credential with `GetSecretValue`. **Keep the bootstrap
key, or an admin credential, somewhere you can reach when the server is
gone** — a password manager entry alongside the offline copy of the restic
password. A backup you cannot authenticate to is not a backup.
