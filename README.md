## Deployment of Flask Application to the Azure Web App
```cmd

az appservice plan create --name flask-webapp-npic --resource-group random-testing --sku B1 --is-linux


az webapp create --resource-group random-testing --plan flask-webapp-npic --name doc-generator-npic --runtime "PYTHON|3.11"


az webapp config set -g random-testing -n doc-generator-npic --startup-file "bash -lc \"exec gunicorn --bind 0.0.0.0:$PORT app:app\""


az webapp config appsettings set -g random-testing -n doc-generator-npic --settings ^
SCM_DO_BUILD_DURING_DEPLOYMENT=true ^
PLAYWRIGHT_BROWSERS_PATH=/home/site/wwwroot/.playwright ^
"POST_BUILD_COMMAND=python -m playwright install chromium"

az webapp deploy -g random-testing -n doc-generator-npic --src-path app.zip
az webapp deploy -g random-testing -n doc-generator-npic --src-path app.zip --type zip --clean true
```

In the KUDU Bash:
```cmd
cd site/wwwroot

cat <<'EOF' > /home/site/wwwroot/startup.sh
#!/usr/bin/env bash
set -e

# Persist Playwright browsers under /home
export PLAYWRIGHT_BROWSERS_PATH=/home/site/wwwroot/.playwright

# Create/activate a virtualenv under persisted /home
VENV=/home/site/wwwroot/antenv
if [ ! -d "$VENV" ]; then
  python -m venv "$VENV"
fi
source "$VENV/bin/activate"

# Upgrade pip and install your app's dependencies
python -m pip install --upgrade pip
python -m pip install --no-cache-dir -r /home/site/wwwroot/requirements.txt

# Install Playwright OS dependencies + Chromium
python -m playwright install-deps
python -m playwright install chromium

# Start Gunicorn (bind to $PORT for App Service health checks)
exec "$VENV/bin/gunicorn" \
  --bind 0.0.0.0:${PORT:-8000} \
  --workers 1 \
  --timeout 600 \
  --access-logfile - \
  --error-logfile - \
  --log-level debug \
  app:app
EOF
chmod +x /home/site/wwwroot/startup.sh

```