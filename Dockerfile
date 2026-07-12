FROM python:3.14-slim

# gosu lets the entrypoint drop privileges to PUID:PGID at runtime.
# curl is the FTPS upload engine for ftp_tools: it does TLS session reuse on the
# data channel, which devices like Bambu Lab printers require and ftplib can't do.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies (baked into the image at build time → fast, reproducible startup)
COPY app/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY app/ ./

# Privilege-dropping entrypoint
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8787

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "server.py"]
