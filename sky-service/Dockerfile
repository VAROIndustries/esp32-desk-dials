# JPL DE421 (Moon and planets, 1900-2050), checked against a pinned SHA-256: a mismatch fails the build.
FROM python:3.12-slim AS ephemeris
ARG DE421_URL=https://ssd.jpl.nasa.gov/ftp/eph/planets/bsp/de421.bsp
ARG DE421_SHA256=a20a7139da04cbc462454634918e9a9ca69127044e2cc9d4f9c16e238d2deedc
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL --retry 3 -o /de421.bsp "$DE421_URL" \
    && echo "$DE421_SHA256  /de421.bsp" | sha256sum -c - \
    && chmod 444 /de421.bsp

FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=ephemeris /de421.bsp /app/data/de421.bsp
COPY app.py engine.py elements.py planets.py screens.py skymath.py store.py bench.py ./
ENV PYTHONUNBUFFERED=1 PORT=8080 DATA_DIR=/data
RUN useradd -u 10001 -M -s /usr/sbin/nologin dial && mkdir /data && chown dial /data
USER dial
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request as u;u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8080/health',timeout=4)"
CMD ["python", "app.py"]
