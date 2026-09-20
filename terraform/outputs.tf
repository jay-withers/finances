output "resource_group_name" {
  description = "This project's resource group."
  value       = azurerm_resource_group.this.name
}

# The whole point of the deployment: the URL opened on a phone. Taken from the
# ingress block rather than `latest_revision_fqdn`, which changes with every
# revision and so is the wrong thing to bookmark.
output "app_url" {
  description = "The application's stable HTTPS URL. Bookmark this one; it survives deploys."
  value       = "https://${azurerm_container_app.this.ingress[0].fqdn}"
}

output "container_app_name" {
  description = "Name of the container app, which `make deploy` passes to `az containerapp update`."
  value       = azurerm_container_app.this.name
}

output "digest_job_name" {
  description = "Name of the reminder digest job, for `make deploy` and for starting a run by hand with `az containerapp job start`."
  value       = azurerm_container_app_job.digest.name
}

output "key_vault_name" {
  description = "Key Vault name, for populating APP-PASSCODE, RESEND-API-KEY and DIGEST-EMAIL-TO with `az keyvault secret set` — see `make secrets`."
  value       = azurerm_key_vault.this.name
}

output "identity_client_id" {
  description = "Client ID of the workload identity, which both workloads receive as `AZURE_CLIENT_ID` and use to reach Key Vault and the document."
  value       = azurerm_user_assigned_identity.this.client_id
}

# What `make deploy` pushes onto the running revision, because `common_env` sits
# under `ignore_changes` and Terraform will therefore never update it itself.
output "state_container_url" {
  description = "Blob container holding the finances document, for `make deploy` and for `finances import` run locally."
  value       = "${azurerm_storage_account.state.primary_blob_endpoint}${azurerm_storage_container.state.name}"
}

# --- the custom domain --------------------------------------------------------
#
# Both are empty when var.custom_domain_name is unset, so `make url` falls back
# to app_url and `make dns` prints nothing to do.

output "custom_domain_url" {
  description = "The bound custom domain, if var.custom_domain_name is set. Empty otherwise."
  value       = var.custom_domain_name != "" ? "https://${var.custom_domain_name}" : ""
}

# The value of the asuid.<label> TXT record. Azure checks it during binding to
# prove the domain is ours, and it is stable for the life of the app.
output "custom_domain_verification_id" {
  description = "Domain verification ID, published as the `asuid.<label>` TXT record before apply."
  value       = azurerm_container_app.this.custom_domain_verification_id
  sensitive   = true
}
