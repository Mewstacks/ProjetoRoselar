from django.utils import timezone


REMEMBER_DURATION = 30 * 24 * 60 * 60
RENEW_INTERVAL = 24 * 60 * 60
REMEMBER_RENEWED_AT = "remember_renewed_at"


def remember_session(session):
    session.set_expiry(REMEMBER_DURATION)
    session[REMEMBER_RENEWED_AT] = int(timezone.now().timestamp())


def renew_remembered_session(session):
    if session.get_expiry_age() == REMEMBER_DURATION:
        last_renewal = session.get(REMEMBER_RENEWED_AT, 0)
        if timezone.now().timestamp() - last_renewal >= RENEW_INTERVAL:
            remember_session(session)
