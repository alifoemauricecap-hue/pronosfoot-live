# PronoFoot Live 2.0 — image de déploiement universelle
# Compatible : Hugging Face Spaces (Docker), Render, Railway, Fly.io, Koyeb, VPS...
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static ./static

# Hugging Face Spaces attend le service sur le port 7860 ; les autres
# plateformes injectent leur propre variable PORT automatiquement.
ENV PORT=7860
EXPOSE 7860

CMD ["python", "app.py"]
