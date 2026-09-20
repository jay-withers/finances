TF_DIR := terraform
ENV ?= dev

IMAGE_REGISTRY ?= ghcr.io/jay-withers/finances
# Defaults to the local commit, which is what you want when iterating: build,
# push, and the tag you just built is the one you reference.
IMAGE_TAG ?= $(shell git rev-parse --short HEAD)
# `file` means the ?= default fired rather than the caller passing one.
IMAGE_TAG_EXPLICIT := $(filter-out file,$(origin IMAGE_TAG))

.DEFAULT_GOAL := help

.PHONY: help install lint test run seed import show digest build push deploy url logs secrets dns bind-domain init fmt validate plan apply

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# Expected to be re-run after a dev container rebuild, not just after a clone:
# uv installs into ~/.local/bin, which is the container's writable layer and
# does not survive one.
install: ## Install pre-commit hooks and Python dependencies
	pre-commit install
	pre-commit install --hook-type commit-msg
	command -v uv >/dev/null || curl -fsSL https://astral.sh/uv/install.sh | sh
	uv sync --extra dev

test: ## Run the test suite
	# --extra dev: pytest is an extra, not a dependency group, so `uv run` does
	# not install it. Without this the target only works after `make install`.
	uv run --extra dev pytest

lint: ## Run every pre-commit hook against every file
	pre-commit run --all-files

# No Azure at all: with no STATE_CONTAINER_URL the document falls back to a
# local file, and APP_PASSCODE is read from the environment before Key Vault is
# ever consulted. Browsers treat localhost as a secure origin, so the session
# cookie works over plain http here despite being set `Secure`.
run: ## Serve locally on :8000 against a local document
	APP_PASSCODE=$${APP_PASSCODE:-local} uv run finances serve --reload

# The local file starts empty, which is the one state the app has least to
# show: no pots, so no payday screen worth looking at and an empty dashboard.
# This writes a sample document in exactly the shape the blob holds.
#
# STATE_CONTAINER_URL is cleared rather than merely expected to be unset: a
# developer with it exported for `make show` would otherwise get a refusal here,
# and the refusal is a guard against overwriting the real document, not a
# workflow.
seed: ## Write a sample document to the local file (FORCE=1 replaces an existing one)
	@STATE_CONTAINER_URL= uv run finances seed $(if $(FORCE),--force,) || { \
		echo "hint: make seed FORCE=1   # replaces the existing local document" >&2; \
		exit 1; }

# The one-off that migrates the spreadsheet. Defaults to the local document so
# the numbers can be checked before anything touches Azure; pass REMOTE=1 to
# import into the deployed blob, which needs Storage Blob Data Contributor on
# the container — which whoever applied the Terraform has.
import: ## Import the spreadsheet (FILE=path, REMOTE=1 targets the deployed blob)
	@if [ -z "$(FILE)" ]; then echo "error: pass FILE=/path/to/Finances_3.xlsx" >&2; exit 1; fi
	$(if $(REMOTE),STATE_CONTAINER_URL="$$(terraform -chdir=$(TF_DIR) output -raw state_container_url)",STATE_CONTAINER_URL=) \
		uv run --extra dev finances import "$(FILE)" $(if $(FORCE),--force,)

show: ## Print the deployed document as JSON
	STATE_CONTAINER_URL="$$(terraform -chdir=$(TF_DIR) output -raw state_container_url)" \
		uv run finances show

# What the scheduled job does, without sending. Reads whichever document
# STATE_CONTAINER_URL points at, local file included.
digest: ## Print the reminder digest without sending it
	uv run finances digest --dry-run

# `--platform linux/amd64` is not optional. Container Apps runs amd64 only and
# this dev host is arm64, so a native build deploys an image that crash-loops
# with an exec format error and no other clue.
build: ## Build the image for linux/amd64 (set IMAGE_TAG, defaults to the git SHA)
	docker buildx build --platform linux/amd64 --load \
		-t $(IMAGE_REGISTRY)/finances:$(IMAGE_TAG) .

push: ## Push the image to ghcr.io (needs write:packages)
	gh auth token | docker login ghcr.io -u $$(gh api user --jq .login) --password-stdin
	docker push $(IMAGE_REGISTRY)/finances:$(IMAGE_TAG)

# A bare `make deploy` is a hard error, unlike build/push. Those default the tag
# to the local git SHA, which is what you want when iterating. Deploying is
# different: the default would silently roll the app onto whatever commit
# happens to be checked out, which may never have been pushed to ghcr.io at all.
#
# Both workloads are rolled together. They run the same image and differ only in
# `args`, so a deploy that moved one and not the other would leave the digest
# reading a document written by a newer schema.
deploy: ## Roll an image tag onto the app and the digest job (IMAGE_TAG required)
	@if [ -z "$(IMAGE_TAG_EXPLICIT)" ]; then \
		echo "error: pass a tag explicitly, e.g. make deploy IMAGE_TAG=v0.1.0" >&2; exit 1; fi
	@case "$(IMAGE_TAG)" in latest|main|unset) \
		echo "error: $(IMAGE_TAG) is a moving tag. Container Apps only creates a revision when the template changes, so re-pushing one deploys nothing and reports success." >&2; exit 1;; esac
	terraform -chdir=$(TF_DIR) init -reconfigure -backend-config=backends/$(ENV).hcl
	az containerapp update \
		--name "$$(terraform -chdir=$(TF_DIR) output -raw container_app_name)" \
		--resource-group "$$(terraform -chdir=$(TF_DIR) output -raw resource_group_name)" \
		--image $(IMAGE_REGISTRY)/finances:$(IMAGE_TAG) \
		--set-env-vars IMAGE_TAG=$(IMAGE_TAG) \
		STATE_CONTAINER_URL="$$(terraform -chdir=$(TF_DIR) output -raw state_container_url)"
	az containerapp job update \
		--name "$$(terraform -chdir=$(TF_DIR) output -raw digest_job_name)" \
		--resource-group "$$(terraform -chdir=$(TF_DIR) output -raw resource_group_name)" \
		--image $(IMAGE_REGISTRY)/finances:$(IMAGE_TAG) \
		--set-env-vars IMAGE_TAG=$(IMAGE_TAG) \
		STATE_CONTAINER_URL="$$(terraform -chdir=$(TF_DIR) output -raw state_container_url)"

url: ## Print the application's URL
	@custom="$$(terraform -chdir=$(TF_DIR) output -raw custom_domain_url)"; \
		if [ -n "$$custom" ]; then echo "$$custom"; \
		else terraform -chdir=$(TF_DIR) output -raw app_url; echo; fi

logs: ## Tail the deployed app's logs
	az containerapp logs show \
		--name "$$(terraform -chdir=$(TF_DIR) output -raw container_app_name)" \
		--resource-group "$$(terraform -chdir=$(TF_DIR) output -raw resource_group_name)" \
		--container finances --follow

secrets: ## Print the az commands that populate this project's Key Vault
	@vault="$$(terraform -chdir=$(TF_DIR) output -raw key_vault_name)"; \
		echo "az keyvault secret set --vault-name $$vault --name APP-PASSCODE --value <passcode>"; \
		echo "az keyvault secret set --vault-name $$vault --name RESEND-API-KEY --value <key>"; \
		echo "az keyvault secret set --vault-name $$vault --name DIGEST-EMAIL-TO --value <you@example.com>"

# Both records must resolve *before* the apply that creates the certificate:
# Azure validates them during issuance, not after.
dns: ## Print the DNS records the custom domain needs
	@host="$$(terraform -chdir=$(TF_DIR) output -raw custom_domain_url | sed 's|^https://||')"; \
		if [ -z "$$host" ]; then echo "no custom_domain_name set; nothing to do"; exit 0; fi; \
		label="$${host%%.*}"; \
		echo "CNAME  $$label  ->  $$(terraform -chdir=$(TF_DIR) output -raw app_url | sed 's|^https://||')"; \
		echo "TXT    asuid.$$label  ->  $$(terraform -chdir=$(TF_DIR) output -raw custom_domain_verification_id)"

# The apply cannot finish the binding itself: ARM needs the managed
# certificate's id in the bind request and the provider has no field to put it
# in (hashicorp/terraform-provider-azurerm#27362, open). Run this after any
# apply that recreated the custom domain or the certificate. A plan afterwards
# reports no diff.
bind-domain: ## Finish binding the managed certificate (manual, see #27362)
	@host="$$(terraform -chdir=$(TF_DIR) output -raw custom_domain_url | sed 's|^https://||')"; \
		if [ -z "$$host" ]; then echo "no custom_domain_name set; nothing to do"; exit 0; fi; \
		app="$$(terraform -chdir=$(TF_DIR) output -raw container_app_name)"; \
		rg="$$(terraform -chdir=$(TF_DIR) output -raw resource_group_name)"; \
		env_id="$$(az containerapp show --name "$$app" --resource-group "$$rg" --query properties.environmentId -o tsv)"; \
		cert_id="$$(az containerapp env certificate list --ids "$$env_id" --managed-certificates-only \
			--query "[?properties.subjectName=='$$host'].id | [0]" -o tsv)"; \
		az containerapp hostname bind --name "$$app" --resource-group "$$rg" \
			--hostname "$$host" --certificate "$$cert_id" --environment "$$env_id"

init: ## terraform init, without configuring the state backend
	terraform -chdir=$(TF_DIR) init -backend=false

fmt: ## terraform fmt -recursive
	terraform -chdir=$(TF_DIR) fmt -recursive

validate: init ## terraform init + validate (no Azure credentials needed)
	terraform -chdir=$(TF_DIR) validate

plan: ## terraform init + plan
	terraform -chdir=$(TF_DIR) init -reconfigure -backend-config=backends/$(ENV).hcl
	terraform -chdir=$(TF_DIR) plan -var-file=environments/$(ENV).tfvars

apply: ## terraform init + apply
	terraform -chdir=$(TF_DIR) init -reconfigure -backend-config=backends/$(ENV).hcl
	terraform -chdir=$(TF_DIR) apply -var-file=environments/$(ENV).tfvars
