# The reminder digest.
#
# This is what replaces the two calendar reminders: it reads the same document
# the app does, works out what needs attention, and emails it. The dashboard
# shows the same list on every visit — the job exists so that not visiting is
# not the same as not knowing.
#
# Cron expressions are evaluated in **UTC**, so the wall-clock time shifts by an
# hour with British Summer Time. `schedule_trigger_config` is ForceNew: changing
# the schedule replaces the job rather than updating it.
resource "azurerm_container_app_job" "digest" {
  name                         = module.naming_digest.container_app_job.name
  container_app_environment_id = data.azurerm_container_app_environment.platform.id
  resource_group_name          = azurerm_resource_group.this.name
  # Unlike a container app, a job **requires** location and the provider rejects
  # it being omitted. Same region as the environment either way.
  location = azurerm_resource_group.this.location

  # Ten minutes against a run that reads one blob and makes one HTTPS call. The
  # bound is there to stop a hung request holding a replica open, not because
  # the work is long.
  replica_timeout_in_seconds = 600
  replica_retry_limit        = 1

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.this.id]
  }

  schedule_trigger_config {
    cron_expression          = var.digest_cron_expression
    parallelism              = 1
    replica_completion_count = 1
  }

  template {
    container {
      name   = "digest"
      image  = local.image
      cpu    = local.container_cpu
      memory = local.container_memory

      # No `command` — see the note on azurerm_container_app.this. This is the
      # exact resource shape that took market-agent down.
      args = ["digest"]

      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  tags = local.tags

  # Same split as the container app: `make deploy` (az cli) owns the image and
  # env after the first revision. See that resource for why the whole env map is
  # ignored rather than one entry.
  lifecycle {
    ignore_changes = [
      template[0].container[0].image,
      template[0].container[0].env,
    ]
  }
}
