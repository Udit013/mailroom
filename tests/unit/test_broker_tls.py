import ssl

from mailroom.celery_app import broker_ssl


def test_amqps_checks_the_certificate_and_the_host_name():
    options = broker_ssl("amqps://user:secret@broker.example.com/vhost")

    assert options == {"cert_reqs": ssl.CERT_REQUIRED, "server_hostname": None}


def test_plain_amqp_has_no_tls_options():
    assert broker_ssl("amqp://user:secret@rabbitmq:5672//") is None


def test_each_call_gets_its_own_options():
    """kombu writes the host name into the dict, so sharing one would leak between hosts."""
    first = broker_ssl("amqps://a.example.com")
    second = broker_ssl("amqps://b.example.com")

    assert first is not second
