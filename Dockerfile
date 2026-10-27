# Use an official Python runtime as a parent image
FROM python:3.14-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file to the working directory
COPY requirements.txt .

# Install dependencies
RUN pip install -r requirements.txt

# Copy the current directory contents into the container at /app
COPY . .
RUN sed -i 's/\r$//' entrypoint.sh

# Make port 8000 available to the world outside this container
EXPOSE 8000
ENV PYTHONUNBUFFERED=1

# Run entrypoint.sh (creates tables and EXTERNAL accounts, then starts the API).
# Called through sh so it works even if the file lacks the executable bit.
CMD ["sh", "./entrypoint.sh"]