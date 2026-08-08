# Two repositories, one per image. `force_delete = true` is not a nicety: an ECR
# repository refuses to delete while it still holds images, and the API image is
# ~4 GB of them. Without this flag `terraform destroy` stops halfway and you finish
# the teardown by hand in the console (V13).

resource "aws_ecr_repository" "api" {
  name         = "backline-api"
  force_delete = true

  image_scanning_configuration {
    scan_on_push = false
  }

  tags = { Name = "backline-api" }
}

resource "aws_ecr_repository" "ui" {
  name         = "backline-ui"
  force_delete = true

  image_scanning_configuration {
    scan_on_push = false
  }

  tags = { Name = "backline-ui" }
}
