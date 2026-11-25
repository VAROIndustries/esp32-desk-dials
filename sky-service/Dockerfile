FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py engine.py elements.py screens.py skymath.py store.py bench.py ./
ENV PYTHONUNBUFFERED=1 PORT=8080 DATA_DIR=/data
RUN useradd -u 10001 -M -s /usr/sbin/nologin dial && mkdir /data && chown dial /data
USER dial
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request as u;u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8080/health',timeout=4)"
CMD ["python", "app.py"]
