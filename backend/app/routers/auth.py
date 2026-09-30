from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.repositories import users
from app.schemas import LoginRequest, RegisterRequest, TokenResponse, UserResponse
from app.security import create_access_token, hash_password, verify_password

router = APIRouter()


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=201,
)
def register_endpoint(request: RegisterRequest, session: Session = Depends(get_db)):
    normalized_email = str(request.email).strip().lower()
    existing_user = users.get_user_by_email(session, normalized_email)
    if existing_user is not None:
        raise HTTPException(
            status_code=409,
            detail="Email is already registered",
        )
    password_hash = hash_password(request.password)
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
