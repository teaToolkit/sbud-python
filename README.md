# sbud for Python

The Python client for the sbud service, for Python 3.10 and later, with no dependencies. `SbudClient` has a method for each route of the service's API: sandboxes and changes to them, chats, consoles, the kit catalogue, logins and sign-ins, diagnostics and repairs, settings, tasks and the event stream. Answers come back as the service's JSON, in dictionaries and lists with the API's own names. A refused request raises `SbudError`, which lists every faulty field when there are several.

```python
from sbud import SbudClient

client = SbudClient()
for sandbox in client.sandboxes():
	print(sandbox["name"], sandbox["state"])

task = client.wait_for_task(client.create_sandbox(client.defaults_for_folder("/home/me/shop")))
print(task["state"], task["lines"][-1])
```

The service has not been released yet. This client has been tested against the service on Linux, with a stand-in for Docker Sandboxes; the Windows named-pipe path has not been run.
