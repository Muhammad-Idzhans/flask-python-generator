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






---
---
---
---
---
---
---
---
---
---






## Deployment latest using Playwright (CMD)
#### 1. Create a Web App plan
- The web app plan is basic and uses Linux.
```cmd
az appservice plan create --name flask-webapp-npic --resource-group random-testing --sku B1 --is-linux
```

#### 2. Create Azure Web App Resource
- For the `web-app-name`, you can choose any desire name that you want.
- Make sure that the name is all in **lower case**, and **no space**.
```cmd
az webapp create --resource-group <resource-group-name> --plan flask-webapp-npic --name <web-app-name> --runtime "PYTHON|3.11"
```

#### 3. Create a `requirements.txt`:
```txt
azure-identity==1.25.0
azure-ai-agents==1.1.0
azure-ai-projects==1.0.0
azure-core==1.36.0
azure-storage-blob==12.27.0
certifi==2025.10.5
cffi==2.0.0
charset-normalizer==3.4.4
cryptography==46.0.3
Flask==3.1.2
gunicorn==23.0.0
idna==3.11
isodate==0.7.2
pycparser==2.23
pdfkit==1.0.0
Jinja2==3.1.6
requests==2.32.5
typing_extensions==4.15.0
urllib3==2.5.0
python-dotenv==1.1.1
pdf2docx==0.5.8
PyMuPDF==1.26.4
matplotlib==3.8.0
seaborn==0.13.0
numpy==1.26.0
playwright==1.55.0
```

#### 4. Create a `dockerfile` in the project:
```docker
# Playwright base image with browsers & OS deps installed
# (This image simplifies Chromium on Azure. If v1.55.0 tag is unavailable,
# use the closest 'jammy' or 'noble' variant and run "python -m playwright install chromium".)
FROM mcr.microsoft.com/playwright/python:v1.55.0-jammy

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Copy and install deps (you've already removed pypandoc; good)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Ensure browsers match the Python package version
RUN python -m playwright install chromium

# Copy your Flask code (adjust names if different)
COPY . /app

# Default output dir (overridden by Azure App Settings below)
ENV OUTPUT_DIR=/app/outputs \
    PORT=8000

EXPOSE 8000
CMD [ "bash", "-lc", "gunicorn -w 4 -b 0.0.0.0:${PORT} app:app" ]
```

#### 5. Build & Push  Docker Image
- Make sure the docker desktop is open
```docker
docker build -t <dockerhub-username>/doc-generator-npic:1.55 .
docker login
docker push <dockerhub-username>/doc-generator-npic:1.55
```

#### 6. Configure Azure Web App for Containers
```cmd
az webapp config container set ^
  -g random-testing -n doc-generator-npic ^
  --container-image-name <dockerhub-username>/doc-generator-npic:1.55 ^
  --container-registry-url https://index.docker.io
```

#### 6. Set App Settings
```cmd
az webapp config appsettings set -g random-testing -n doc-generator-npic --settings ^
  WEBSITES_ENABLE_APP_SERVICE_STORAGE=true ^
  WEBSITES_PORT=8000 ^
  OUTPUT_DIR=/home/site/wwwroot/outputs ^
  PROJECT_ENDPOINT="https://<your-ai-project-endpoint>" ^
  MODEL_DEPOYMENT_NAME="<your-model-deployment-name>" ^
  API_KEY="<your-ai-api-key>"
```

#### 7.Enable Managed Identity & Assign Roles
- Enable managed identity inside the Azure Web App Configuraton.
- Open your Azure Web App -> Settigs -> Identity -> On
- Other than that, you can also do it in the cmd:
```cmd
az webapp identity assign -g <resource-group> -n <webapp-name>
```
- After that, on Azure AI Foundry Project in the Azure Portal, assign **Azure AI User** role to the Web App's identity.

#### 8. Increase Gunicorn Timeout:
- Long-running reports need more than 30s
```cmd
az webapp config appsettings set -g <resource-group> -n <web-app-name> --settings ^
  GUNICORN_CMD_ARGS="--timeout 600 --workers 2 --worker-class gthread --threads 8"
```

#### 9. Restart and Verify:
```
az webapp restart -g <resource-group> -n <web-app-name>
az webapp log tail -g <resource-group> -n <web-app-name>
```

#### 10. Enable KUDU to always open:
```cmd
az webapp config set -g random-testing -n doc-generator-npic --always-on true
```

#### 11. Test Endpoints:
- Open your Postman to test the API endpoints.
- Health:
```
GET https://<your-app>.azurewebsites.net/health
```
- Report:
```
POST https://<your-app>.azurewebsites.net/api/report?format=links
```

## Steps for updating the app
#### 1. Make your code changes locally (eg., update app.py)

#### 2. Rebuild the Docker Image:
```cmd
docker build -t <docker-username>/doc-generator-npic:latest .
```

#### 3. Push the new image to Docker Hub:
- Make sure your docker desktop is turned on
```cmd
docker push <docker-username>/doc-generator-npic:latest
```

#### 4. Update Azure Web App to use the new tagL:
```cmd
az webapp config container set ^
  -g <resource-group> -n <web-app-name> ^
  --container-image-name <docker-username>/doc-generator-npic:latest ^
  --container-registry-url https://index.docker.io
```

#### 5. Restart the Web App:
```cmd
az webapp restart -g <resource-group> -n <web-app-name>
```

