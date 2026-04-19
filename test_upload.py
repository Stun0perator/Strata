import requests

files = {'file': open('test.svg', 'rb')}
r = requests.post('http://127.0.0.1:8000/api/svg/upload', files=files)
print(r.status_code)
print(r.json())
