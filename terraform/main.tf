# This project's own infrastructure. The Container Apps environment it runs on
# is **not** here — it is shared, lives in jay-withers/azure-container-apps, and
# is resolved by name in data.tf.
#
# Everything this project owns is in its own resource group with its own Key
# Vault, identity and storage, so the household's finances and the passcode
# guarding them are readable by nothing else on the platform.
module "naming" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver
  # (version below), not a git source — there's no commit hash to pin.
  source  = "Azure/naming/azurerm"
  version = "~> 0.4"
  suffix  = [var.project_name, var.environment]
}

# A second instance for the digest job, suffixed with its workload. Unlike
# gym-log — which has one container app and nothing that parses its name — the
# platform's job-failure alert splits on JobName_s, so a job's name has to carry
# which workload it is: caj-<project>-<env>-<workload>.
#
# `caj-finances-dev-digest` is 23 of the 32 characters container apps allow. The
# naming module truncates silently rather than failing, so check with
# `terraform console` before renaming either part.
module "naming_digest" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver, as above.
  source  = "Azure/naming/azurerm"
  version = "~> 0.4"
  suffix  = [var.project_name, var.environment, "digest"]
}

# The resource group is github-repos' (`create_resource_groups`), which
# imported the one this config's first apply made, and grants the pipeline
# Contributor and conditioned RBAC Administrator on it. Dropped from this state
# without touching Azure; data.tf looks it up by the same name.
removed {
  from = azurerm_resource_group.this

  lifecycle {
    destroy = false
  }
}
