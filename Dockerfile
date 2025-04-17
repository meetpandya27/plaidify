FROM python:3.9-slim

# Create and set the working directory
WORKDIR /app

# Copy only requirements first to leverage Docker cache
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . /app/

# Expose the default FastAPI port
EXPOSE 8000

# Run the Uvicorn server
CMD ["gunicorn", "src.main:app", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000"]