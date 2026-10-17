from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import commit_session, get_db
from app.invitations import (
    InvitationUnavailableError,
    prepare_redemption,
    redeem_invitation,
)
from app.repositories import users
from app.schemas import LoginRequest, RegisterRequest, TokenResponse, UserResponse
from app.security import create_access_token, hash_password, verify_password

router = APIRouter()


def _register_with_invitation(
    registration: RegisterRequest, email: str, session: Session
) -> UserResponse:
    try:
        code_hash = prepare_redemption(
            session,
            email=email,
            code=registration.invitation_code.get_secret_value()
            if registration.invitation_code is not None
            else None,
        )
        # End the preflight read before expensive hashing or a write transaction.
        session.rollback()
        password_hash = hash_password(registration.password)
        user = redeem_invitation(
            session, email=email, code_hash=code_hash, password_hash=password_hash
        )
        response = UserResponse(id=user.id, email=user.email)
        commit_session(session)
    except (InvitationUnavailableError, IntegrityError):
        session.rollback()
        raise HTTPException(
            status_code=403, detail="Invitation is invalid or unavailable"
        ) from None
    except Exception:
        session.rollback()
        raise
    return response


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=201,
)
def register_endpoint(
    registration: RegisterRequest,
    request: Request,
    session: Session = Depends(get_db),
):
    mode = request.app.state.registration_mode
    if mode not in {"open", "invite"}:
        raise HTTPException(status_code=403, detail="Registration is closed")
    normalized_email = str(registration.email).strip().lower()
    if mode == "invite":
        return _register_with_invitation(registration, normalized_email, session)
    existing_user = users.get_user_by_email(session, normalized_email)
    if existing_user is not None:
        raise HTTPException(
            status_code=409,
            detail="Email is already registered",
        )
    password_hash = hash_password(registration.password)
    try:
        user = users.create_user(
            session,
            normalized_email,
            password_hash,
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Email is already registered",
        )
    return UserResponse(
        id=user.id,
        email=user.email,
    )


@router.post(
    "/login",
    response_model=TokenResponse,
)
def login_endpoint(
    login_request: LoginRequest,
    request: Request,
    session: Session = Depends(get_db),
) -> TokenResponse:
    normalized_email = str(login_request.email).strip().lower()
    user = users.get_user_by_email(session, normalized_email)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password",
        )
    if not verify_password(login_request.password, user.password_hash):
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password",
        )
    access_token = create_access_token(
        user_id=user.id,
        secret_key=request.app.state.jwt_secret_key,
        expires_delta=request.app.state.access_token_expires_delta,
    )

    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
    )
