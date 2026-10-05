# Official image version documented at https://playwright.dev/python/docs/docker
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
WORKDIR /app
COPY backend /app/backend
COPY deploy/renderer /app/renderer
RUN pip install --no-cache-dir playwright==1.63.0 'fastapi>=0.115,<1' 'uvicorn>=0.32,<1' 'pydantic>=2.10,<3'
USER pwuser
CMD ["uvicorn", "renderer.app:app", "--host", "0.0.0.0", "--port", "8088"]
