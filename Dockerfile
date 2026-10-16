FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080
ENV XSETTLERS_HOST=0.0.0.0
ENV XSETTLERS_REQUIRE_REAL_TOKENS=1

CMD ["python", "-m", "xsettlers_mcp.server"]
