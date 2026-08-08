# One evidence bucket with three real uses — ALB access logs (written here by the
# load balancer), the 17 MB pg_dump from A3 (provenance for the migration claim),
# and the extracted eval artifacts from A5.4. None of the three is decoration; a
# bucket that existed only to appear in a diagram would be worse than no bucket.
#
# `force_destroy = true` because S3 refuses to delete a bucket that still holds
# objects, and all three uses put objects in it (V13).

resource "aws_s3_bucket" "evidence" {
  bucket        = "backline-evidence-${data.aws_caller_identity.current.account_id}"
  force_destroy = true

  tags = { Name = "backline-evidence" }
}

# Versioning is deliberately left off (a new bucket is unversioned by default):
# every object here is written once and read once, and versioning would only make
# `force_destroy` slower at teardown by leaving delete markers to sweep.

resource "aws_s3_bucket_server_side_encryption_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------------------------------------------------------------------
# ALB access-log delivery permissions.
#
# Two statements on purpose. us-west-2 predates August 2022, so AWS documents the
# regional ELB *account* as the writing principal — that is the first statement,
# resolved via the data source rather than hardcoded (it is 797873946194 here).
# AWS has since also enabled the `logdelivery.elasticloadbalancing.amazonaws.com`
# *service* principal, which is the second statement. Granting both costs nothing
# and means this policy does not silently break if the ALB switches which identity
# it delivers under.
#
# Neither statement grants anything public — the public access block above stays
# fully on, and `block_public_policy` would reject this policy if it did.
# ---------------------------------------------------------------------------

data "aws_elb_service_account" "main" {}

data "aws_iam_policy_document" "evidence_alb_logs" {
  statement {
    sid     = "AlbLogDeliveryAccountWrite"
    effect  = "Allow"
    actions = ["s3:PutObject"]

    principals {
      type        = "AWS"
      identifiers = [data.aws_elb_service_account.main.arn]
    }

    resources = ["${aws_s3_bucket.evidence.arn}/alb/AWSLogs/${data.aws_caller_identity.current.account_id}/*"]
  }

  statement {
    sid     = "AlbLogDeliveryServiceWrite"
    effect  = "Allow"
    actions = ["s3:PutObject"]

    principals {
      type        = "Service"
      identifiers = ["logdelivery.elasticloadbalancing.amazonaws.com"]
    }

    resources = ["${aws_s3_bucket.evidence.arn}/alb/AWSLogs/${data.aws_caller_identity.current.account_id}/*"]

    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
  }

  statement {
    sid     = "AlbLogDeliveryAclCheck"
    effect  = "Allow"
    actions = ["s3:GetBucketAcl"]

    principals {
      type        = "Service"
      identifiers = ["logdelivery.elasticloadbalancing.amazonaws.com"]
    }

    resources = [aws_s3_bucket.evidence.arn]
  }
}

resource "aws_s3_bucket_policy" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  policy = data.aws_iam_policy_document.evidence_alb_logs.json

  # The policy must exist before the ALB is created: enabling access logs makes the
  # load balancer test-write into the bucket, and ALB creation fails outright if
  # that write is denied.
  depends_on = [aws_s3_bucket_public_access_block.evidence]
}
