#!/usr/bin/env bash
set -e

echo "=================================================="
echo "🚀 Signalement V6.1 Platform Installation"
echo "=================================================="

# Check Docker prerequisite
if ! command -v docker &> /dev/null; then
    echo "❌ Error: 'docker' is required but not installed."
    exit 1
fi

# Check Docker Compose prerequisite
if ! docker compose version &> /dev/null && ! command -v docker-compose &> /dev/null; then
    echo "❌ Error: 'docker-compose' is required but not installed."
    exit 1
fi

echo "✅ Prerequisites verified."

# Create .env file if missing
if [ ! -f .env ]; then
    echo "⚙️ Generating secure production .env configuration..."
    JWT_SECRET_GEN=$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || echo "signalement_secret_$(date +%s)")
    DB_PASS_GEN=$(python3 -c "import secrets; print(secrets.token_hex(16))" 2>/dev/null || echo "db_pass_$(date +%s)")
    
    cat <<ENVEOF > .env
NODE_ENV=production
PORT=3000
JWT_SECRET=${JWT_SECRET_GEN}
POSTGRES_DB=signpim
POSTGRES_USER=postgres
POSTGRES_PASSWORD=${DB_PASS_GEN}
DB_HOST=postgres
DB_PASSWORD=${DB_PASS_GEN}
DB_NAME=signpim
REDIS_HOST=localhost
DATABASE_URL=postgresql://postgres:${DB_PASS_GEN}@postgres:5432/signpim
PYTHON_ENGINE_URL=http://python-engine:8000
ENVEOF
    echo "✅ .env created with auto-generated secure credentials."
else
    echo "ℹ️ Existing .env file found."
fi

# Create Nginx SSL dummy directory if missing
mkdir -p nginx/ssl
if [ ! -f nginx/ssl/cert.pem ]; then
    echo "🔒 Generating self-signed SSL certificate for local HTTPS testing..."
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
      -keyout nginx/ssl/key.pem \
      -out nginx/ssl/cert.pem \
      -subj "/C=DK/ST=Denmark/L=Copenhagen/O=Signalement/CN=localhost" 2>/dev/null || true
fi

# Create Nginx config if missing
mkdir -p nginx
if [ ! -f nginx/nginx.conf ]; then
    cat <<'NGINXEOF' > nginx/nginx.conf
events { worker_connections 1024; }

http {
    upstream backend {
        server backend:3000;
    }

    upstream python_engine {
        server python-engine:8000;
    }

    server {
        listen 80;
        server_name localhost;

        location /health {
            proxy_pass http://backend/health;
        }

        location /api/v1/execute {
            proxy_pass http://python_engine/api/v1/execute;
        }

        location / {
            proxy_pass http://backend;
        }
    }
}
NGINXEOF
fi

echo "🐳 Building and starting Docker containers..."
if docker compose version &> /dev/null; then
    docker compose up -d --build
else
    docker-compose up -d --build
fi

echo "⏳ Waiting for services to initialize..."
sleep 5

echo "=================================================="
echo "🎉 Signalement V6.1 is successfully installed & running!"
echo "   REST API:       http://localhost/api/products"
echo "   Python Engine:  http://localhost/api/v1/execute"
echo "   Health Check:   http://localhost/health"
echo "=================================================="
