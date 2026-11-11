# sbud for Python

The Python client for the sbud service, for Python 3.10 and later. This early preview has one call, `service()` on `SbudClient`, which reads the running service's version. The service itself has not been released yet, so the preview has been tested only against a stand-in service.

```python
from sbud import SbudClient

print(SbudClient().service().version)
```
