# Backend configuration for the dev environment.
#
#   terraform init -backend-config=backends/dev.hcl
#
# These values are not secret: the state container is protected by Azure RBAC,
# not by keeping its name private. The storage account is shared with the other
# Terraform root configurations and was created once by hand; the container is
# per-repository and must exist before the first init — see the README.
resource_group_name  = "rg-tfstate-shared"
storage_account_name = "sttfsharedjw"
container_name       = "finances"
key                  = "dev.terraform.tfstate"

# OIDC in CI; Entra auth rather than a storage account access key.
use_oidc         = true
use_azuread_auth = true
