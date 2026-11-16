environment = "dev"

custom_domain_name = "finances.jaywithers.uk"

# The cloudflare repo's backup bucket, and the owner's age public key (its
# private half is in their password manager, nowhere else). The same key as
# gym-log's, so one key restores every app.
backup_bucket        = "jw-app-backups"
backup_r2_endpoint   = "https://4984ee9defcc3f275ef47f477222beba.eu.r2.cloudflarestorage.com"
backup_age_recipient = "age1fkakxklnyl37d64j8e3g3y3llsgjqrc9cqpch7ltkp3gef7a2qmqwvq7ry"
