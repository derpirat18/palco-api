# PostgreSQL (RDS) y Valkey (ElastiCache), en las subredes privadas, cifrados
# en disco y en transito.

# --- PostgreSQL ---------------------------------------------------------------

# Solo letras y numeros: la contrasena viaja dentro de DATABASE_URL, y un
# simbolo (%, @, /) romperia la URL o el archivo de Alembic.
resource "random_password" "postgres" {
  length  = 40
  special = false
}

resource "aws_db_subnet_group" "palco" {
  name       = local.nombre
  subnet_ids = aws_subnet.privada[*].id
  tags       = { Name = local.nombre }
}

resource "aws_db_instance" "palco" {
  identifier     = local.nombre
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.postgres.clase

  db_name  = "palco"
  username = "palco"
  password = random_password.postgres.result
  port     = 5432

  allocated_storage     = var.postgres.disco_gb
  max_allocated_storage = var.postgres.disco_maximo_gb
  storage_type          = "gp3"
  storage_encrypted     = true

  multi_az               = var.postgres.multi_az
  db_subnet_group_name   = aws_db_subnet_group.palco.name
  vpc_security_group_ids = [aws_security_group.postgres.id]
  publicly_accessible    = false

  # PostgreSQL 16 en RDS exige conexion cifrada (rds.force_ssl = 1): la app
  # se conecta con DB_SSL=true.
  backup_retention_period    = var.postgres.dias_de_respaldo
  backup_window              = "08:00-09:00" # 2-3 a. m. en Mexico
  maintenance_window         = "Tue:09:00-Tue:10:00"
  auto_minor_version_upgrade = true
  copy_tags_to_snapshot      = true

  deletion_protection       = var.postgres.proteger_de_borrar
  skip_final_snapshot       = !var.postgres.proteger_de_borrar
  final_snapshot_identifier = var.postgres.proteger_de_borrar ? "${local.nombre}-final" : null

  apply_immediately = var.entorno == "sandbox"

  tags = { Name = local.nombre }
}

# --- Valkey ------------------------------------------------------------------

resource "random_password" "valkey" {
  length  = 48
  special = false
}

resource "aws_elasticache_subnet_group" "palco" {
  name       = local.nombre
  subnet_ids = aws_subnet.privada[*].id
}

# Sin modo cluster: los scripts Lua del apartado tocan varias llaves a la
# vez y en modo cluster Redis/Valkey los rechaza (CROSSSLOT).
resource "aws_elasticache_replication_group" "palco" {
  replication_group_id = local.nombre
  description          = "Apartados, limites y cache de Palco (${var.entorno})"

  engine               = "valkey"
  engine_version       = "7.2"
  parameter_group_name = "default.valkey7"
  node_type            = var.valkey.clase
  port                 = 6379

  num_cache_clusters         = var.valkey.nodos
  automatic_failover_enabled = var.valkey.nodos > 1
  multi_az_enabled           = var.valkey.nodos > 1

  subnet_group_name  = aws_elasticache_subnet_group.palco.name
  security_group_ids = [aws_security_group.valkey.id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token                 = random_password.valkey.result

  snapshot_retention_limit = var.valkey.dias_de_copias
  apply_immediately        = var.entorno == "sandbox"

  tags = { Name = local.nombre }
}
