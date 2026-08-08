# One provider, one region, one tag block. Every resource in this root module
# inherits Project/Ephemeral from default_tags — which is what makes the teardown
# proof in §A6.3 a one-liner:
#   aws resourcegroupstaggingapi get-resources --tag-filters Key=Project,Values=backline
# An empty list there is the receipt that nothing was orphaned.
provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "backline"
      Ephemeral = "true"
    }
  }
}

# Account id for the evidence bucket name and the ALB log-delivery policy path.
# Read-only. (The region is taken from var.region everywhere rather than a data
# source, since it is pinned by variable and one source of truth beats two.)
data "aws_caller_identity" "current" {}
