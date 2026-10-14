FROM python:3.12-slim
RUN pip install --no-cache-dir requests
WORKDIR /app
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["auto_bridge_signal.py"]
