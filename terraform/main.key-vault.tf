# This project's own vault. Because it holds only this project's secrets, the
# identity's grant below can be vault-wide without widening anything: there is
# nothing else in here to read.
#
# Three secrets live here, all set by hand (`make secrets`) and none of them
# managed by Terraform:
#
#   APP-PASSCODE     the shared passcode guarding the app
#   RESEND-API-KEY   sends the reminder digest
#   DIGEST-EMAIL-TO  comma-separated recipients; absent means the digest skips
resource "azurerm_key_vault" "this" {
  # checkov:skip=CKV_AZURE_42: purge protection deliberately off — see below.
  # checkov:skip=CKV_AZURE_110: same.
  # checkov:skip=CKV_AZURE_109: no network ACLs — see CKV_AZURE_189.
  # checkov:skip=CKV_AZURE_189: public access stays enabled. Restricting it needs a
  #   private endpoint (a standing monthly cost, and there is no VNet — the shared
  #   Container Apps environment is Consumption-only) or a static egress IP a
  #   scale-to-zero container app does not have.
  # checkov:skip=CKV2_AZURE_32: no private endpoint, as above.
  # `.name_unique`, not `.name`: vault names are DNS-based and globally unique
  # across every Azure tenant, not just this subscription, so `kv-finances-dev`
  # collides with whoever else in the world picked that name first — it did, on
  # the first apply. The naming module's random suffix, generated once and
  # fixed in state from then on, is what makes this actually available.
  name                = module.naming.key_vault.name_unique
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  tenant_id           = data.azurerm_client_config.current.tenant_id
  sku_name            = "standard"

  # RBAC rather than access policies: the assignments below are then ordinary
  # role assignments, visible and auditable like every other permission.
  rbac_authorization_enabled = true

  # Off deliberately, so `terraform destroy` actually removes this rather than
  # leaving a soft-deleted vault holding a globally unique name. Paired with
  # `purge_soft_delete_on_destroy` in the provider block.
  purge_protection_enabled   = false
  soft_delete_retention_days = 7

  # Stated rather than left to the provider default, because the skip above
  # claims it: a default that changed would silently contradict the comment.
  public_network_access_enabled = true

  tags = local.tags
}

# The app and the job read their secrets at runtime through this.
resource "azurerm_role_assignment" "identity_secrets_user" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.this.principal_id
  # Stated explicitly: without it the provider looks the principal up in the
  # directory, which fails intermittently on an identity created moments ago.
  principal_type = "ServicePrincipal"
}

# Whoever applies can then populate the values. Terraform deliberately creates
# **no** `azurerm_key_vault_secret`: no secret value belongs in source or in
# state. See the README for the `az keyvault secret set` calls.
resource "azurerm_role_assignment" "deployer_secrets_officer" {
  for_each = toset(concat(
    [data.azurerm_client_config.current.object_id],
    var.key_vault_administrator_object_ids,
  ))

  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = each.value
}
