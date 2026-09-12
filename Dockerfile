FROM python:3.12-slim
WORKDIR /app
COPY server/hub.py ./hub.py
EXPOSE 8090
ENV PORT=8090
CMD ["python", "hub.py"]
