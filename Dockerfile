FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir aiogram aiohttp beautifulsoup4 lxml python-dotenv aiomysql cryptography
COPY . .
CMD ["python", "main.py"]
