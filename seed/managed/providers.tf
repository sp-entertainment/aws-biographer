provider "aws" {
  region = var.region

  # NO default_tags, deliberately.
  #
  # A default_tags block would stamp SeedStack onto every resource, including
  # the ones this stack creates specifically to be *untagged*. An agent that
  # flags untagged resources would then find none, and the seeded mess would
  # silently stop producing the finding it exists to produce.
  #
  # Cleanup does not need the tag: every resource here is Terraform-managed, so
  # `terraform destroy` removes all of it, and outputs.tf lists every id and ARN
  # for manual verification. See README.md.
}
