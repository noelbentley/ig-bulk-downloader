"""Desktop error categories; HTTP restrictions never imply permission to log in."""
from dataclasses import dataclass
import re

import requests
from instaloader import exceptions as errors


def safe_activity(message):
    message = re.sub(r"https?://\S+", "[URL omitted]", str(message), flags=re.IGNORECASE)
    message = re.sub(r"\b(?:authorization|cookie)[\"']?\s*:\s*[^\r\n]+", "[authentication header omitted]", message, flags=re.IGNORECASE)
    return re.sub(r"\b(password|sessionid|csrftoken|token|authorization|cookie)[\"']?\s*[:=]\s*[\"']?[^\s,;}\]]+",
                  r"\1=[redacted]", message, flags=re.IGNORECASE)


class SessionExpired(errors.LoginRequiredException):
    pass


@dataclass(frozen=True)
class Failure:
    kind: str
    message: str


def classify_error(error, authenticated=False):
    from desktop.services.safety import RestrictionStopped
    if isinstance(error, RestrictionStopped):
        if error.review:
            return Failure("restricted", "Repeated restriction or security check: manual review is required in Settings before continuing.")
        return Failure("rate_limit" if error.status == 429 else "restricted",
                       f"Instagram returned HTTP {error.status}. Downloads stopped; its restriction cooldown must finish before further requests.")
    if isinstance(error, SessionExpired):
        return Failure("session_expired", "Saved session expired or is no longer valid. Sign in again or choose another session.")
    if isinstance(error, errors.PrivateProfileNotFollowedException):
        return Failure("private_not_followed", "Private account not followed. Login does not grant access; the selected Instagram account must already have permission to view this profile.")
    if isinstance(error, (errors.ProfileNotExistsException, errors.QueryReturnedNotFoundException)):
        return Failure("unavailable", "Content unavailable or profile not found. Check the username; Instagram may not expose it publicly.")
    if isinstance(error, errors.BadCredentialsException):
        return Failure("credentials", "Instagram rejected the login. Check the username and password.")
    if isinstance(error, errors.TooManyRequestsException):
        return Failure("rate_limit", "Instagram rate limit reached. Downloads stopped; wait before retrying.")
    message = str(error).lower()
    if isinstance(error, (errors.AbortDownloadException, errors.ConnectionException)):
        if re.search(r"\b429\b", message):
            return Failure("rate_limit", "Instagram rate limit reached (HTTP 429). Downloads stopped; respect its cooldown before retrying.")
        if any(word in message for word in ("checkpoint_required", "challenge_required", "feedback_required")):
            return Failure("restricted", "Instagram requires a security check or has restricted requests. Complete checks in Instagram and respect its cooldown.")
        if re.search(r"\b(?:400|403)\b", message):
            return Failure("restricted", "Instagram rejected or restricted the request (HTTP 400/403). This does not establish that login is required; requests have stopped.")
        if re.search(r"\b401\b", message) or "logged out" in message or "redirected to login" in message:
            return classify_error(SessionExpired() if authenticated else errors.LoginRequiredException())
    if isinstance(error, errors.LoginRequiredException):
        if authenticated:
            return classify_error(SessionExpired())
        return Failure("authentication_required", "Authentication required for this content. A session with permission to view it is needed.")
    if isinstance(error, errors.AbortDownloadException):
        return Failure("restricted", "Instagram stopped the request for a security check or access restriction. Check Instagram and wait before retrying.")
    if isinstance(error, (errors.QueryReturnedForbiddenException, errors.QueryReturnedBadRequestException)):
        return Failure("restricted", "Instagram restricted this request (HTTP 400/403). Login is not assumed to be the solution.")
    if isinstance(error, (errors.ConnectionException, requests.exceptions.RequestException)):
        return Failure("network", "Network failure while contacting Instagram. Check your connection and try later.")
    if isinstance(error, errors.LoginException):
        return Failure("credentials", "Instagram could not complete login. Complete any security challenge in Instagram, then try again.")
    if isinstance(error, OSError):
        return Failure("local", "Local file error. Check the download folder, free space and file permissions.")
    return Failure("unexpected", f"Unexpected {type(error).__name__}; the download was stopped. No request details were logged.")
