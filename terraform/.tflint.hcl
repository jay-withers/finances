config {
  call_module_type = "local"
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "azurerm" {
  enabled = true
  version = "0.32.0"
  source  = "github.com/terraform-linters/tflint-ruleset-azurerm"
}

# This rule wants `lifecycle { prevent_destroy = true }` on stateful resources.
# The storage account holding the finances document has it and should. The Key
# Vault deliberately does not: its three secrets are re-settable by hand in
# seconds, and prevent_destroy there would make `terraform destroy` fail outright
# while protecting nothing irreplaceable. Excluding the one type rather than
# disabling the rule keeps the guard armed for anything added later.
rule "azurerm_resources_missing_prevent_destroy" {
  enabled = true
  exclude = [
    "azurerm_key_vault",
  ]
}
