from .models import Job
from .services.jobs import service_status


def services(request):
    """Worker / stream health for the header indicator (only for logged-in pages)."""
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    status = service_status()
    worker, stream = status["worker"], status["stream"]
    busy = Job.objects.filter(status=Job.Status.RUNNING).exists()
    return {
        "svc_worker_ok": bool(worker and (worker.is_alive or busy)),
        "svc_stream_ok": bool(stream and stream.is_alive),
        "svc_queue": Job.objects.filter(status=Job.Status.QUEUED).count(),
    }
