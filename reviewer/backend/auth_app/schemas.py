"""Pydantic request/response schemas for the auth service."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, EmailStr, Field


# ── Requests ──────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: EmailStr
    password: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str | None = None


class ContributorRegisterRequest(BaseModel):
    firstName: str
    lastName: str = ""
    email: EmailStr
    password: str
    confirmPassword: str | None = None
    contributorType: str | None = None
    countryOfResidence: str | None = None
    dateOfBirth: str | None = None
    timeZone: str | None = None
    weeklyAvailabilityHours: str | None = None
    departmentCategory: str | None = None
    primarySkills: list[str] = Field(default_factory=list)
    secondarySkills: list[str] = Field(default_factory=list)
    otherSkills: list[str] = Field(default_factory=list)
    phone: str | None = None
    degree: str | None = None
    branch: str | None = None
    linkedin: str | None = None
    careerStage: str | None = None
    yearsExperience: str | None = None
    workStart: str | None = None
    workEnd: str | None = None
    ndaSignatoryLegalName: str | None = None
    segment: str | None = "general"
    gender: str | None = None
    requiresApproval: bool = False
    # Women-in-Tech application (collected when requiresApproval/segment=women).
    applicationOrg: str | None = None
    applicationBackground: str | None = None
    applicationDocUrl: str | None = None
    # Uploaded supporting files (images/PDF) → Vercel Blob descriptors.
    applicationDocs: list[dict[str, Any]] = Field(default_factory=list)
    mentorGuideAcknowledged: bool = False
    acceptTermsOfUse: bool = False
    acceptCodeOfConduct: bool = False
    acceptPrivacyPolicy: bool = False
    acceptHarassmentPolicy: bool = False
    acknowledgmentsAccepted: bool = False
    notifyNewTasksOptIn: bool = False
    marketingOptIn: bool = False


class EnterpriseRegisterRequest(BaseModel):
    firstName: str
    lastName: str = ""
    email: EmailStr
    password: str
    orgName: str
    orgType: str | None = None
    industry: str | None = None
    companySize: str | None = None
    adminTitle: str | None = None
    website: str | None = None
    hqCountry: str | None = None
    hqCity: str | None = None
    phone: str | None = None
    adminDept: str | None = None
    acceptTos: bool = False
    acceptPp: bool = False
    acceptEsa: bool = False
    acceptAhp: bool = False
    marketingOptIn: bool = False


class MfaCodeRequest(BaseModel):
    code: str


class MfaRecoveryRequest(BaseModel):
    recovery_code: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str
    confirmPassword: str | None = None


class ChangePasswordRequest(BaseModel):
    new_password: str
    confirmPassword: str | None = None
    old_password: str | None = None


class OtpSendRequest(BaseModel):
    email: EmailStr | None = None
    phone: str | None = None


class OtpVerifyRequest(BaseModel):
    email: EmailStr | None = None
    phone: str | None = None
    code: str


# ── Responses ──────────────────────────────────────────────────────────────────

class UserOut(BaseModel):
    id: str
    firstName: str
    lastName: str
    email: str
    role: str
    emailVerified: bool = False
    phoneVerified: bool = False
    mfaEnabled: bool = False
    phone: str | None = None
    adminTitle: str | None = None
    approvalStatus: str = "approved"


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


def user_row_to_out(row: dict[str, Any]) -> UserOut:
    return UserOut(
        id=str(row.get("id")),
        firstName=row.get("first_name") or "",
        lastName=row.get("last_name") or "",
        email=row.get("email") or "",
        role=row.get("role") or "contributor",
        emailVerified=bool(row.get("email_verified")),
        phoneVerified=bool(row.get("phone_verified")),
        mfaEnabled=bool(row.get("mfa_enabled")),
        phone=row.get("phone"),
        adminTitle=row.get("admin_title"),
        approvalStatus=row.get("approval_status") or "approved",
    )
