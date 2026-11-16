# The nightly off-Azure backup (`finances backup`, src/finances/backup.py): an
# age-encrypted copy of the document in the cloudflare repo's R2 bucket,
# downloaded again and decrypted with the drill key on every run to prove it
# restores. Adapted from gym-log's main.container-apps-job.backup.tf.
#
# A job inside this app rather than a GitHub workflow because this identity
# already reads the document: a workflow would need a new grant on the
# household's finances for the pipeline identity. Running here also puts the
# job under the platform's job-failure alert and observability's heartbeat
# check (both learn jobs by name) with nothing added there.
resource "azurerm_container_app_job" "backup" {
  name                         = module.naming_backup.container_app_job.name
  container_app_environment_id = data.azurerm_container_app_environment.platform.id
  resource_group_name          = data.azurerm_resource_group.this.name

  # See the digest job's identical arguments.
  workload_profile_name = "Consumption"
  location              = data.azurerm_resource_group.this.location

  # One small document, two uploads and two downloads. Five minutes is mostly
  # the cold start; the retry covers a transient R2 or Key Vault error, and a
  # second copy under a new timestamped key is harmless.
  replica_timeout_in_seconds = 300
  replica_retry_limit        = 1

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.this.id]
  }

  schedule_trigger_config {
    # 02:45 UTC: after the day's edits, and fifteen minutes after gym-log's so
    # the two don't cold-start together.
    cron_expression          = "45 2 * * *"
    parallelism              = 1
    replica_completion_count = 1
  }

  template {
    container {
      name   = "backup"
      image  = local.image
      cpu    = local.container_cpu
      memory = local.container_memory

      # No `command`: see the note on azurerm_container_app.this.
      args = ["backup"]

      dynamic "env" {
        for_each = local.backup_env
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  tags = local.tags

  # As the digest job: `make deploy` owns the image and the env.
  lifecycle {
    ignore_changes = [
      template[0].container[0].image,
      template[0].container[0].env,
    ]
  }
}
