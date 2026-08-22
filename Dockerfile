FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Создаём папки для файлов
RUN mkdir -p uploads

CMD ["python", "main.py"]
