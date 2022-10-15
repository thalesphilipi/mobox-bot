FROM python:3.9
COPY . /
WORKDIR /
RUN pip3 install -r requirements.txt
EXPOSE 8080
CMD ["python3","main.py"]