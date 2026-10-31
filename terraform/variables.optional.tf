variable "project_name" {
  description = "Short name for this project, used by the naming module for every resource."
  type        = string
  default     = "finances"

  validation {
    # Lowercase only (container apps reject uppercase), and short enough that
    # `caj-<project>-<env>-digest` fits the 32 characters container apps allow —
    # the naming module truncates silently rather than failing. At `finances`
    # that job name is 23 of 32. Changing this is also how to resolve a clash on
    # the globally unique Key Vault or storage account name.
    condition     = can(regex("^[a-z][a-z0-9]{1,14}$", var.project_name))
    error_message = "project_name must be 2-15 lowercase alphanumerics starting with a letter."
  }
}

variable "tags" {
  description = "Tags merged over the defaults in locals.tf."
  type        = map(string)
  default     = {}
}

# --- the shared platform ------------------------------------------------------
#
# Resolved by name rather than from the platform's Terraform state. These
# defaults match jay-withers/azure-container-apps at its own defaults; run
# `make outputs` there to confirm.

variable "platform_resource_group_name" {
  description = "Resource group holding the shared Container Apps environment."
  type        = string
  default     = "rg-platform-dev"
}

variable "platform_environment_name" {
  description = "Name of the shared Container Apps environment this app runs on."
  type        = string
  default     = "cae-platform-dev"
}

variable "platform_app_insights_name" {
  description = "Name of the shared Application Insights instance the app reports telemetry to."
  type        = string
  default     = "appi-platform-dev"
}

# --- the digest job -----------------------------------------------------------

variable "digest_cron_expression" {
  description = "When the reminder digest runs, in **UTC** — Container Apps jobs have no timezone setting. The default is 17:00 UTC every Sunday: 18:00 UK time under BST, 17:00 under GMT, so anything that needs attention surfaces at the end of the weekend, ready for the week ahead, rather than waiting for a fixed date."
  type        = string
  default     = "0 17 * * 0"
}

# --- the custom domain --------------------------------------------------------

variable "custom_domain_name" {
  description = "Hostname to bind to the app with a free Azure-managed certificate, e.g. `finances.jaywithers.uk`. Empty creates neither the certificate nor the binding; the default `*.azurecontainerapps.io` URL always works either way. The CNAME (to that default FQDN) and the `asuid.<label>` TXT record (holding `custom_domain_verification_id`) must already resolve before apply, because Azure validates both during issuance — `make dns` prints them."
  type        = string
  default     = ""

  validation {
    # An apex domain cannot be a CNAME, and CNAME is the only validation method
    # these resources use. Caught here rather than as an opaque DNS failure
    # midway through certificate issuance.
    condition     = var.custom_domain_name == "" || length(split(".", var.custom_domain_name)) > 2
    error_message = "custom_domain_name must be a subdomain, not an apex domain: validation is by CNAME, which an apex record cannot hold."
  }
}
