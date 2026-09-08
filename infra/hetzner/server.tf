# Register the SSH public key with Hetzner
resource "hcloud_ssh_key" "default" {
  name       = var.ssh_key_name
  public_key = file(pathexpand(var.ssh_public_key_path))
}

# Default .env content used when env_file_content is not provided
locals {
  default_env = <<EOF
INHERENT_VERSION=${var.inherent_version}
POSTGRES_USER=postgres
POSTGRES_PASSWORD=changeme
POSTGRES_DB=knowledge_base
MONGODB_URI=mongodb://mongodb:27017
MONGODB_DB_NAME=main
WEAVIATE_URL=http://weaviate:8080
# public-api image must send this key as Bearer to Weaviate — env alone is not enough
WEAVIATE_API_KEY=changeme
REDIS_URL=redis://valkey:6379
MQ_REDIS_URL=redis://valkey:6379
AWS_ACCESS_KEY_ID=S3RVER
AWS_SECRET_ACCESS_KEY=S3RVER
AWS_REGION=us-east-1
AWS_S3_REGION=us-east-1
AWS_S3_BUCKET=inherent-documents
AWS_S3_ENDPOINT=http://s3rver:9000
EMBEDDING_MODEL_ID=BAAI/bge-small-en-v1.5
EMBEDDING_SERVICE_URL=http://text-embeddings-inference:80
EMBEDDING_DIM=384
EMBEDDING_ENABLED=true
TEMPORAL_ENABLED=true
TEMPORAL_HOST=temporal:7233
TEMPORAL_NAMESPACE=default
TEMPORAL_TASK_QUEUE=document-ingestion
INGESTION_API_KEY=changeme
LOG_LEVEL=INFO
ENVIRONMENT=production
EOF

  env_content = var.env_file_content != "" ? var.env_file_content : local.default_env

  env_content_b64 = base64encode(local.env_content)

  cloud_init = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    env_content_b64 = local.env_content_b64
    compose_git_ref = var.compose_git_ref
  })
}

# Create the Hetzner server
resource "hcloud_server" "default" {
  name        = var.server_name
  server_type = var.server_type
  image       = var.server_image
  location    = var.server_location
  backups     = var.server_backups
  ssh_keys    = [hcloud_ssh_key.default.id]
  user_data   = local.cloud_init

  labels = {
    environment = var.environment
    service     = "inherent"
  }
}
