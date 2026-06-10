# Backend Build Spec — every frontend API contract (mentor-portal-v2)

Generated from the fresh `mentor-portal-v2` frontend. Canonical backend = `backends/`. DB = Neon Postgres; audit = MongoDB.

**Totals:** 190 endpoints — 81 exist, 50 missing, 36 prisma-only (86 to build/wire).

Legend: [E]=backend-exists  [M]=backend-missing (BUILD)  [P]=prisma-only (BUILD backend + repoint FE)  [L]=frontend-local (no backend needed)

| Status | Methods | FE Route | Backend role | Backend path | Request | Response |
|---|---|---|---|---|---|---|
| L | GET,POST | /api/auth/[...nextauth] | auth-shared | NEXTAUTH_HANDLERS | NextAuth credential/provider flows (Google, Microsoft, Crede | JWT, session cookie, NextAuth redirect/callback |
| E | POST | /api/auth/forgot-password | auth-shared | /api/v1/auth/password/forgot | { email: string } | { message?: string; detail?: string } or confirmation |
| E | POST | /api/auth/mfa-confirm | auth-shared | /api/v1/auth/mfa/setup/init, /api/v1/auth/mfa/setup/confirm, /api/v1/auth/mfa/verify, /api/v1/auth/login | { email?: string; password?: string; code?: string; action?: | { phase: 'setup'/'verify'/'done'; qr_uri?: string; secret?:  |
| E | GET | /api/auth/oauth/authorize | auth-shared | /api/v1/auth/oauth/{provider}/authorize | ?provider=google/microsoft&redirectAfter=...&role=...&intent | 302 redirect to backend OAuth authorize URL with state blob |
| E | POST | /api/auth/oauth/exchange | auth-shared | /api/v1/auth/oauth/{provider}/callback | { code: string; provider: 'google'/'microsoft'; state?: stri | { access_token: string; refresh_token: string; token_type: s |
| P | POST | /api/auth/oidc/[tenantSlug]/callback | super-admin | PRISMA | { code?: string; claims?: { ... } } (mock input for OIDC flo | { ok: boolean; userId: string; sessionId: string; jitProvisi |
| E | POST | /api/auth/otp/send-email | auth-shared | /api/v1/auth/otp/send-email | { email: string } or { ... } | { message?: string; detail?: string } or OTP confirmation |
| E | POST | /api/auth/otp/send-phone | auth-shared | /api/v1/auth/otp/send-phone | { phone: string } or { ... } | { message?: string; detail?: string } or OTP confirmation |
| E | POST | /api/auth/otp/verify-email | auth-shared | /api/v1/auth/otp/verify-email | { email: string; code: string } | { verified: boolean; message?: string } or error |
| E | POST | /api/auth/otp/verify-phone | auth-shared | /api/v1/auth/otp/verify-phone | { phone: string; code: string } | { verified: boolean; message?: string } or error |
| E | POST | /api/auth/password/change | auth-shared | /api/v1/auth/password/change | { old_password: string; new_password: string; confirmPasswor | { success?: boolean; message?: string } or error detail |
| E | POST | /api/auth/password/reset | auth-shared | /api/v1/auth/password/reset | { token: string; new_password: string } | { message?: string; success?: boolean } |
| E | POST | /api/auth/register | auth-shared | /api/v1/auth/register | { firstName: string; lastName: string; email: string; passwo | { user: GlimmoraUser } |
| P | POST | /api/auth/register/mentor | mentor | PRISMA | { firstName: string; lastName: string; email: string; passwo | { ok: boolean; userId: string; email: string } |
| P | POST | /api/auth/register/reviewer | super-admin | PRISMA | { firstName: string; lastName: string; email: string; passwo | { ok: boolean; userId: string; email: string } |
| P | POST | /api/auth/saml/[tenantSlug]/callback | super-admin | PRISMA | { attributes?: { ... } } (mock SAML response) | { ok: boolean; userId: string; sessionId: string; jitProvisi |
| E | POST | /api/auth/sso-intent | auth-shared | /api/v1/auth/sso-intent | { tenant_slug?: string; email?: string } | { sso_enabled: boolean; kind?: 'oidc'/'saml'; ... } |
| E | GET | /api/auth/sso/discover | auth-shared | /api/v1/auth/sso/discover | ?email=user@domain.com | { tenant_slug?: string; sso_enabled: boolean; kind?: 'oidc'/ |
| E | POST | /api/auth/validate | auth-shared | /api/v1/auth/validate | { email?: string; password?: string } | { valid: boolean } or { available: boolean } |
| E | GET | /api/auth/contributor/oauth/authorize-url | freelancer | /api/v1/auth/contributor/oauth/{provider}/authorize | ?provider=google/microsoft | { url: string } (OAuth authorize URL) |
| E | GET | /api/me | super-admin | /api/v1/me | (no body, auth header required) | { user: { id, email, name, role, initials }; tenant?: { id,  |
| P | GET,POST | /api/mentor/me | mentor | PRISMA | GET: none / POST: none | { profile: { ... }; role: string; isSeniorOrLead: boolean; o |
| M | GET | /api/sessions | auth-shared | /api/v1/sessions | (auth header required, no body) | { sessions: UserSessionRecord[] } or UserSessionRecord[] |
| M | DELETE | /api/sessions/[sessionId] | auth-shared | /api/v1/sessions/{sessionId} | DELETE with sessionId param | { success: boolean } or 204 No Content |
| P | GET | /api/contributor/tasks | freelancer | PRISMA | status?: string; limit?: number | items: TaskDefinition[]; total: number |
| P | GET | /api/contributor/tasks/[taskId] | freelancer | PRISMA | taskId: string | task: { id, title, status, submissions[] } |
| E | POST | /api/contributor/tasks/[taskId]/accept | freelancer | /api/v1/contributor/tasks/{taskId}/accept | empty | timeline: Event[] |
| E | POST | /api/contributor/tasks/[taskId]/decline | freelancer | /api/v1/contributor/tasks/{taskId}/decline | reason?: string | data: { decline_reason } |
| E | GET | /api/contributor/tasks/[taskId]/latest-submission | freelancer | /api/contributor/tasks/{taskId}/latest-submission | taskId: string | submission: { id, version, status, submittedAt } |
| E | GET | /api/contributor/tasks/[taskId]/review-feedback | freelancer | /api/contributor/tasks/{taskId}/review-feedback | taskId: string | feedback: object; status: string |
| E | POST | /api/contributor/tasks/[taskId]/submissions | freelancer | /api/contributor/tasks/{taskId}/submissions | structured_response: object; version: number; notes: string; | submission: { id, task_id, version, submitted_at, status, fi |
| E | POST | /api/contributor/tasks/[taskId]/workroom/uploads | freelancer | /api/contributor/tasks/{taskId}/workroom/uploads | file: FormData; category: string; title: string; description | { id, filename, category, title, uploaded_at } |
| E | DELETE | /api/contributor/tasks/[taskId]/workroom/uploads/[uploadId] | freelancer | /api/contributor/tasks/{taskId}/workroom/uploads/{uploadId} | taskId: string; uploadId: string | null (204) |
| M | GET,POST | /api/submissions | freelancer | /api/v1/submissions | GET: (list); POST: { structured_response, payload } | submissions[] / submission: { id, status } |
| M | GET,PATCH | /api/submissions/[submissionId] | freelancer | /api/v1/submissions/{submissionId} | submissionId: string; PATCH: { notes, version, checklist_ack | submission: { id, status, notes, version } |
| M | POST | /api/submissions/[submissionId]/submit | freelancer | /api/v1/submissions/{submissionId}/submit | submissionId: string; body: object | submission: { id, status: submitted } |
| M | POST | /api/submissions/[submissionId]/withdraw | freelancer | /api/v1/submissions/{submissionId}/withdraw | submissionId: string | submission: { id, status: withdrawn } |
| M | POST | /api/submissions/[submissionId]/artifacts | freelancer | /api/v1/submissions/{submissionId}/artifacts | submissionId: string; artifact: { name, url, mimeType } | artifacts: { id, kind, name, url, mimeType }[] |
| E | GET,PATCH | /api/contributor/submissions/[submissionId] | freelancer | /api/contributor/submissions/{submissionId} | submissionId: string; PATCH: { notes, version } | submission: { id, status, notes, version } |
| E | POST | /api/contributor/submissions/[submissionId]/resubmit | freelancer | /api/contributor/submissions/{submissionId}/resubmit | submissionId: string; notes: string | submission: { id, status: submitted, version, submitted_at } |
| P | GET | /api/contributor/track | freelancer | PRISMA | empty | track: string; contribType: string; onboarding: object |
| E | GET | /api/contributor/search | freelancer | /api/contributor/search | q?: string; limit?: number; x-contributor-id?: string | query: string; total: number; results: object[] |
| P | GET | /api/contributor/account-auth | freelancer | PRISMA | empty | authMode: string; hasPassword: boolean; provider: string; co |
| E | GET,PATCH | /api/contributor/profile | freelancer | /api/v1/users/me/profile | GET: empty; PATCH: { firstName, lastName, bio, skills, locat | profile: { id, firstName, lastName, bio, skills, location } |
| E | PUT | /api/contributor/profile/skills | freelancer | /api/contributor/profile/skills | skills: { id, name, proficiency, years }[] | skills: { id, name, proficiency, years }[] |
| E | GET,POST | /api/contributor/profile/evidence | freelancer | /api/contributor/profile/evidence | GET: q?, type?, skill?; POST: { type, title, description, sk | items: { id, type, title, description, skillTags }[]; total: |
| E | PATCH,DELETE | /api/contributor/profile/evidence/[evidenceId] | freelancer | /api/contributor/profile/evidence/{evidenceId} | evidenceId: string; PATCH: { title, description } | evidence: { id, type, title, description } |
| E | GET | /api/contributor/credentials | freelancer | /api/contributor/credentials | skill?: string; date_filter?: string; page?: number; page_si | credentials: { id, title, issuer, issuedAt }[]; total: numbe |
| E | GET | /api/contributor/credentials/[credentialId] | freelancer | /api/contributor/credentials/{credentialId} | credentialId: string | credential: { id, title, issuer, issuedAt, description } |
| E | GET | /api/contributor/credentials/[credentialId]/certificate | freelancer | /api/contributor/credentials/{credentialId}/certificate | credentialId: string; format?: string | PDF binary OR { url: string } OR base64 string |
| E | POST | /api/contributor/credentials/[credentialId]/share | freelancer | /api/contributor/credentials/{credentialId}/share | credentialId: string; recipientEmail?: string; shareLink?: b | { shareId: string; shareUrl: string; expiresAt: string } |
| E | GET | /api/contributor/credentials/[credentialId]/verification | freelancer | /api/contributor/credentials/{credentialId}/verification | credentialId: string | { verificationId: string; status: string; verifiedAt: string |
| E | POST | /api/contributor/credentials/[credentialId]/academic-portfolio | freelancer | /api/contributor/credentials/{credentialId}/academic-portfolio | credentialId: string; portfolio: object | { credentialId: string; portfolioId: string } |
| E | GET | /api/contributor/credentials/skills/verification | freelancer | /api/contributor/credentials/skills/verification | empty | verifications: { skillId: string; status: string }[] |
| E | GET | /api/contributor/credentials/wallet/cards | freelancer | /api/contributor/credentials/wallet/cards | skill?: string; page?: number; page_size?: number | cards: { id, credentialId, skillId, earnedAt }[]; total: num |
| E | GET,POST | /api/contributor/credentials/wallet/summary | freelancer | /api/contributor/credentials/wallet/summary | GET: empty; POST: { category: string } | { totalCards: number; byCategory: object; updatedAt: string  |
| E | GET | /api/contributor/profile/digital-twin | freelancer | /api/contributor/profile/digital-twin | empty | { tasksCompleted: number; acceptanceRate: number; topSkills: |
| E | GET | /api/contributor/profile/digital-twin/history | freelancer | /api/contributor/profile/digital-twin/history | period?: string (3m/6m/1y) | { period: string; snapshots: object[] } |
| E | GET | /api/contributor/learning/recommendations | freelancer | /api/contributor/learning/recommendations | type?: string; priority?: string; skill?: string | recommendations: { id, type, title, priority, skill }[] |
| E | POST | /api/contributor/learning/recommendations/[recommendationId]/dismiss | freelancer | /api/contributor/learning/recommendations/{recommendationId}/dismiss | recommendationId: string | { recommendationId: string; dismissed: boolean } |
| E | POST | /api/contributor/learning/recommendations/[recommendationId]/mark-opened | freelancer | /api/contributor/learning/recommendations/{recommendationId}/mark-opened | recommendationId: string | { recommendationId: string; opened: boolean } |
| P | POST | /api/contributor/mentorship/opt-in | freelancer | PRISMA | { focus?: string } | { session: object; assigned: boolean } |
| P | GET | /api/contributor/mentorship/status | freelancer | PRISMA | empty | { optedIn: boolean; focus: string; optedInAt: string; upcomi |
| E | GET | /api/contributor/support/faqs | freelancer | /api/contributor/support/faqs | category?: string; q?: string | items: { id, question, answer, category }[]; total: number |
| E | GET,POST | /api/contributor/support/grievances | freelancer | /api/contributor/support/grievances | GET: empty; POST: { subject, description, type } | grievances: { id, subject, status, createdAt }[] OR grievanc |
| E | GET | /api/contributor/support/grievances/[grievanceId] | freelancer | /api/contributor/support/grievances/{grievanceId} | grievanceId: string | grievance: { id, subject, description, status, createdAt } |
| E | POST | /api/contributor/support/safety-reports | freelancer | /api/contributor/support/safety-reports | { type: string; description: string; severity: string } | { reportId: string; status: string; createdAt: string } |
| E | GET,POST | /api/contributor/support/tickets | freelancer | /api/contributor/support/tickets | GET: status?, priority?, category?, page?, page_size?; POST: | tickets: { id, subject, status, priority, createdAt }[] OR t |
| E | GET,PATCH | /api/contributor/support/tickets/[ticketId] | freelancer | /api/contributor/support/tickets/{ticketId} | ticketId: string; PATCH: { status?, priority? } | ticket: { id, subject, description, status, messages[] } |
| E | POST | /api/contributor/support/tickets/[ticketId]/messages | freelancer | /api/contributor/support/tickets/{ticketId}/messages | ticketId: string; { body: string; attachments?: string[] } | message: { id, body, authorId, createdAt } |
| E | GET,PATCH | /api/contributor/profile | freelancer | /api/contributor/profile | GET: none; PATCH: {first_name?, last_name?, phone?, bio?, jo | {id, email, first_name, last_name, name, phone, bio, job_tit |
| E | PUT | /api/contributor/profile/skills | freelancer | /api/contributor/profile/skills | {primary_skills[], secondary_skills[], other_skills[]} | {id, email, primary_skills[], secondary_skills[], other_skil |
| E | GET,POST | /api/contributor/profile/evidence | freelancer | /api/contributor/profile/evidence | GET: q?, type?, skill? (query params); POST: {title, kind, u | GET: {items[], total}; POST: {id, account_id, title, kind, u |
| E | PATCH,DELETE | /api/contributor/profile/evidence/[evidenceId] | freelancer | /api/contributor/profile/evidence/{evidence_id} | PATCH: {title?, kind?, url?}; DELETE: none | PATCH: {id, account_id, title, kind, url, ...}; DELETE: {ok, |
| E | GET | /api/contributor/profile/digital-twin | freelancer | /api/contributor/profile/digital-twin | none | {account_id, skills[], tasks_completed, credentials_count, t |
| E | GET | /api/contributor/profile/digital-twin/history | freelancer | /api/contributor/profile/digital-twin/history | period? (query: '3m'/'6m'/'1y') | {items: [{id, snapshot, created_at}], total, page, page_size |
| M | GET | /api/contributor/credentials | freelancer | /api/contributor/credentials | skill?, date_filter? ('30d'/'90d'/'6m'), page? (default 1),  | {items[], total, page, page_size} |
| E | GET | /api/contributor/credentials/[credentialId] | freelancer | /api/contributor/credentials/{credential_id} | none | {id, account_id, title, issuer, status, data, verification_c |
| E | POST | /api/contributor/credentials/[credentialId]/share | freelancer | /api/contributor/credentials/{credential_id}/share | {...share options/metadata} | {id, share_id, credential_id, account_id, share_url, data, c |
| E | GET | /api/contributor/credentials/[credentialId]/verification | freelancer | /api/contributor/credentials/{credential_id}/verification | none | {credential_id, verification_code, status, verified} |
| E | GET | /api/contributor/credentials/[credentialId]/certificate | freelancer | /api/contributor/credentials/{credential_id}/certificate | format? (default 'pdf', query param) | Binary PDF with Content-Disposition header OR JSON: {credent |
| E | POST | /api/contributor/credentials/[credentialId]/academic-portfolio | freelancer | /api/contributor/credentials/{credential_id}/academic-portfolio | {...academic portfolio data} | {id, account_id, title, issuer, data: {academic_portfolio: { |
| E | GET | /api/contributor/credentials/skills/verification | freelancer | /api/contributor/credentials/skills/verification | none | {verified_skills: {skill_name: 'verified'}, credentials_coun |
| M | GET | /api/contributor/credentials/wallet/cards | freelancer | /api/contributor/credentials/wallet/cards | skill?, page?, page_size? (query params) | {cards: [{id, account_id, title, issuer, status, issued_at,  |
| M | GET,POST | /api/contributor/credentials/wallet/summary | freelancer | /api/contributor/credentials/wallet/summary | GET: none; POST: {category (required), ...} | GET: {total, by_status: {status: count}}; POST: {id, account |
| E | GET | /api/contributor/learning/recommendations | freelancer | /api/contributor/learning/recommendations | type?, priority?, skill? (query params) | [{id, account_id, type, priority, skill, title, description, |
| E | POST | /api/contributor/learning/recommendations/[recommendationId]/dismiss | freelancer | /api/contributor/learning/recommendations/{rec_id}/dismiss | none | {id, account_id, status: 'dismissed', recommendation_id?, di |
| E | POST | /api/contributor/learning/recommendations/[recommendationId]/mark-opened | freelancer | /api/contributor/learning/recommendations/{rec_id}/mark-opened | none | {id, account_id, status: 'opened', recommendation_id?, opene |
| M | GET | /api/payouts | freelancer | /api/v1/payouts | query: page, page_size; headers: Authorization | { items: PayoutDetail[], total: int, page: int, page_size: i |
| M | POST | /api/payouts/[payoutId]/request | freelancer | /api/v1/payouts/{payoutId}/request | body: { payoutMethodId?: string } | { payout: PayoutDetail } |
| M | POST | /api/payouts/[payoutId]/hold | freelancer | /api/v1/payouts/{payoutId}/hold | body: (empty) | { payout: PayoutDetail } |
| M | POST | /api/payouts/[payoutId]/release-hold | freelancer | /api/v1/payouts/{payoutId}/release-hold | body: (empty) | { payout: PayoutDetail } |
| M | POST | /api/payouts/[payoutId]/retry | freelancer | /api/v1/payouts/{payoutId}/retry | body: (empty) | { payout: PayoutDetail } |
| M | GET,POST | /api/payouts/methods | freelancer | /api/v1/payouts/methods | GET: query headers; POST: { kind: 'bank_in'/'upi'/'paypal'/' | { items: PayoutMethodDetail[] } or { method: PayoutMethodDet |
| M | PATCH,DELETE | /api/payouts/methods/[methodId] | freelancer | /api/v1/payouts/methods/{methodId} | PATCH: { setDefault?: boolean }; DELETE: (empty) | { method: PayoutMethodDetail } or { deleted: true } |
| M | GET | /api/payouts/tenant | freelancer | /api/v1/payouts/tenant | query: status?, page?, page_size?; headers: Authorization | { items: PayoutDetail[], total: int } |
| E | GET | /api/contributor/support/faqs | freelancer | /api/contributor/support/faqs | query: category?, q?; headers: Authorization | { items: FAQ[] } where FAQ = { id, question, answer } |
| E | GET,POST | /api/contributor/support/grievances | freelancer | /api/contributor/support/grievances | GET: query; POST: { subject, priority?, ...data } | GET: { items: [], total: 0 }; POST: { id, account_id, kind:  |
| E | GET | /api/contributor/support/grievances/[grievanceId] | freelancer | /api/contributor/support/grievances/{grievanceId} | headers: Authorization Bearer | { id, account_id, kind: 'grievance', subject, priority, stat |
| E | POST | /api/contributor/support/safety-reports | freelancer | /api/contributor/support/safety-reports | headers: Authorization Bearer; body: { subject?, ...reportDa | { id, account_id, kind: 'safety_report', subject, priority:  |
| E | GET,POST | /api/contributor/support/tickets | freelancer | /api/contributor/support/tickets | GET: query: status?, priority?, category?, page?, page_size? | GET: { items: [], page: 1, page_size: 20, total: 0 }; POST:  |
| E | GET,PATCH | /api/contributor/support/tickets/[ticketId] | freelancer | /api/contributor/support/tickets/{ticketId} | GET: headers; PATCH: { status?, subject? } | { id, account_id, kind: 'ticket', subject, priority, status, |
| E | POST | /api/contributor/support/tickets/[ticketId]/messages | freelancer | /api/contributor/support/tickets/{ticketId}/messages | { body, author?: 'contributor' } | { id, ticket_id, account_id, author: 'contributor', body, cr |
| M | GET | /api/notifications | freelancer | /api/v1/notifications | query: page?, page_size?, unreadOnly?; headers: Authorizatio | { notifications: NotificationSummary[], unreadCount: int, to |
| M | PATCH | /api/notifications/[notificationId]/read | freelancer | /api/v1/notifications/{notificationId}/read | body: (empty) | { updated: true, alreadyRead?: boolean } or NotificationSumm |
| M | POST | /api/notifications/mark-all-read | freelancer | /api/v1/notifications/mark-all-read | body: (empty) | { updatedCount: int } |
| L | GET | /api/mock/contributor/earnings/summary | freelancer | PRISMA | none | { withdrawableMinor: int, kpis: { thisWeekMinor, thisMonthMi |
| L | GET | /api/mock/contributor/payouts | freelancer | PRISMA | query: page?, limit? | { items: MockPayout[], total: int, page: int, limit: int } |
| L | PATCH | /api/mentor/settings/notifications | mentor | LOCAL | { rows: [{ key: string, prefs: { inApp: bool, email: bool, s | { success: true } |
| L | GET | /api/mock/mentor/notifications | mentor | PRISMA | none | { items: NotificationSummary[], total: int } |
| E | GET,POST | /api/sow | enterprise | /api/v1/sow | GET: query filters (status, q). POST: projectTitle, clientOr | Array of SOW objects (id, projectTitle, clientOrganisation,  |
| L | POST | /api/sow/proxy | enterprise | GLIMMORA_API (external) | path, method, payload, enterprise flag. Handles token acquis | Forwarded response from Glimmora API endpoint |
| L | GET | /api/sow/token | enterprise | GLIMMORA_API (external) | Query: role (enterprise/contributor), force_refresh flag | { token: string } - Glimmora API Bearer token for direct cli |
| E | GET,PATCH | /api/sow/[sowId] | enterprise | /api/v1/sow/{sowId} | GET: no body. PATCH: partial SOW fields (sections, commercia | SOW object with all fields |
| M | POST | /api/sow/[sowId]/approve | enterprise | /api/v1/sow/{sowId}/approve | Body: stage? (finance/security/legal/platform). Validates us | Updated SOW object with approvalStages[stage].status = appro |
| M | POST | /api/sow/[sowId]/archive | enterprise | /api/v1/sow/{sowId}/archive | No body required | Updated SOW with status = archived |
| M | POST | /api/sow/[sowId]/reject | enterprise | /api/v1/sow/{sowId}/reject | Body: optional comment, stage? | Updated SOW with status = rejected, rejectionReason, decided |
| M | POST | /api/sow/[sowId]/send-back | enterprise | /api/v1/sow/{sowId}/send-back | Body: reason?, stage? | Updated SOW with status = changes_requested, returnedAt, ret |
| M | POST | /api/sow/[sowId]/submit | enterprise | /api/v1/sow/{sowId}/submit | No body required | Updated SOW with status = submitted, submittedAt = ISO times |
| M | POST | /api/sow/[sowId]/withdraw | enterprise | /api/v1/sow/{sowId}/withdraw | Body: reason? | Updated SOW with status = draft, withdrawnAt, withdrawReason |
| P | GET,POST | /api/decomposition/plans | enterprise | PRISMA | POST: sowId, summary?, sourceAgentInvocationId?, structure ( | POST: { plan: {id, sowId, version, status, createdAt, milest |
| P | GET,PATCH | /api/decomposition/plans/[planId] | enterprise | PRISMA | GET: no body. PATCH: summary?, structure? (replaces entire s | { plan: {id, sowId, version, status, summary, milestones[],  |
| P | POST | /api/decomposition/plans/[planId]/activate | enterprise | PRISMA | No body required. Requires permission: activate.decompositio | { plan: {status = active, tasks[].status flipped to ready} } |
| P | POST | /api/decomposition/plans/[planId]/approve | enterprise | PRISMA | No body required. Requires permission: approve.decomposition | { plan: {status = approved, version, approverId, approvedAt} |
| P | POST | /api/decomposition/plans/[planId]/archive | enterprise | PRISMA | No body required. Requires permission: archive.decomposition | { plan: {status = archived, version} } |
| P | POST | /api/decomposition/plans/[planId]/copy | enterprise | PRISMA | No body required. Requires permission: manage.decomposition_ | { plan: {id (new), version, status = draft, sowId (same), mi |
| L | POST | /api/decomposition/proxy | enterprise | GLIMMORA_API (external) | path, method, payload. Handles enterprise MFA service accoun | Forwarded response from Glimmora Decomposition API endpoint |
| P | POST,GET | /api/enterprise/acceptance/[taskId] | enterprise | PRISMA | decision: 'accept'/'rework', note?: string, deciderInitials? | POST: { id: string, decidedAt: string } / GET: { decisions:  |
| P | GET | /api/enterprise/compliance/consent | enterprise | PRISMA | query: search?: string, missing?: 'true'/'false', format?: ' | format=json: { tenantId, total, complete, missing, rows: Arr |
| P | GET | /api/enterprise/compliance/overview | enterprise | PRISMA | none | { tenantId, consent: { totalContributors, withConsent, missi |
| P | GET,PUT | /api/enterprise/compliance/retention | enterprise | PRISMA | PUT: { audit_event?: { mode, days? }, task_evidence?: { mode | { tenantId, rules: RetentionRuleSet, floors: RetentionFloors |
| P | GET,PUT | /api/enterprise/rate-cards | enterprise | PRISMA | PUT: { currency: string, default: number, bySegment?: { stud | { tenantId, tenantCurrency, rateCards: null / RateCardsConfi |
| M | GET | /api/enterprise/review-queue | enterprise | /api/v1/enterprise/review-queue | query params: (not yet specified in FE) | array of review submissions with status, contributor info, s |
| M | GET | /api/enterprise/review-queue/[submissionId] | enterprise | /api/v1/enterprise/review-queue/{submissionId} | none | single submission object with full details |
| M | POST | /api/enterprise/review-queue/[submissionId]/claim | enterprise | /api/v1/enterprise/review-queue/{submissionId}/claim | body: {} (claim-specific payload to be defined) | updated submission with claimed_by, claimed_at fields |
| M | POST | /api/enterprise/review-queue/[submissionId]/decide | enterprise | /api/v1/enterprise/review-queue/{submissionId}/decide | body: { decision: 'approve'/'reject'/'rework', note?: string | updated submission with decision, decidedAt, decisionNote fi |
| M | POST | /api/enterprise/review-queue/[submissionId]/release | enterprise | /api/v1/enterprise/review-queue/{submissionId}/release | body: {} (release-specific payload to be defined) | updated submission with released_by, released_at fields |
| L | GET | /api/enterprise/subscription | enterprise | LOCAL | none | subscription snapshot (resolved from auth session user, fall |
| L | GET | /api/enterprise/subscription/history | enterprise | LOCAL | none | { tenantId, items: Array<subscription plan history entries>  |
| M | POST | /api/enterprise/tasks/[taskId]/assign | enterprise | /api/v1/enterprise/tasks/{taskId}/assign | body: { contributorId?, assigneeEmail?, ... } (exact shape t | updated task with assignee, assignedAt fields |
| M | POST | /api/enterprise/tasks/[taskId]/reassign | enterprise | /api/v1/enterprise/tasks/{taskId}/reassign | body: { fromContributorId?, toContributorId?, reason?, ... } | updated task with new assignee, reassignedAt, reassignmentRe |
| M | GET | /api/enterprise/workforce | enterprise | /api/v1/enterprise/workforce | query: (filters to be defined) | array of workforce members with assignments, skills, availab |
| M | POST | /api/enterprise/workforce/import/preview | enterprise | /api/v1/enterprise/workforce/import/preview | body: FormData / { csvData: string } (bulk import preview) | { previewId, rows: Array<{ email, name, role, status }>, iss |
| M | POST | /api/enterprise/workforce/import/apply | enterprise | /api/v1/enterprise/workforce/import/apply | body: { previewId, confirmImport: boolean } | { imported: number, failed: number, results: Array<{ email,  |
| P | GET | /api/billing/export | enterprise | PRISMA | query: kind: 'payouts'/'billing', from: ISO8601, to: ISO8601 | CSV file with headers: Content-Type: text/csv, Content-Dispo |
| M | GET | /api/mentor/queue | mentor | /api/v1/mentor/queue | query params: status(optional), priority(optional), q(option | { items: Array<{id, title, submission_type, contributor_name |
| M | GET | /api/mentor/me | auth-shared | /api/v1/me | none | { profile, role, isSeniorOrLead, onboardingComplete } - with |
| L | GET,PATCH | /api/mentor/profile | mentor | LOCAL | PATCH body: { bio?(string), mentorshipIntro?(string), langua | { success: true } - stored in runtime-store, not persistent |
| P | POST | /api/mentor/notes | mentor | PRISMA | { sessionId?(string), contributorId?(string), body(string 1- | { note: { id, tenantId, sessionId?, contributorId?, body, vi |
| P | PATCH,DELETE | /api/mentor/notes/[noteId] | mentor | PRISMA | PATCH: { body?(string 1-10000), visibility?(private/shared/p | PATCH: { note: { id, tenantId, body, visibility, mentorUserI |
| P | GET,POST | /api/mentor/sessions | mentor | PRISMA | GET query: status?(string[]), upcomingOnly?(boolean), limit? | GET: { items: Array<enriched session objects>, total }. POST |
| P | GET,POST | /api/mentor/sessions/[sessionId] | mentor | PRISMA | GET: none. POST body: { action(held/no_show/cancel), reason? | GET: { session: enriched session detail }. POST: { session:  |
| L | PATCH | /api/mentor/settings/availability | mentor | LOCAL | { activeDays(string[]), from(string), to(string), timezone(s | { success: true } - stored in runtime-store, not persistent |
| L | PATCH | /api/mentor/settings/notifications | mentor | LOCAL | { rows: Array<{ key(string), prefs: { inApp(boolean), email( | { success: true } - stored in runtime-store, not persistent |
| M | GET | /api/mentor/submissions/[submissionId] | mentor | /api/v1/mentor/submissions/{submissionId} | none | submission detail object (from backend) |
| M | POST | /api/mentor/submissions/[submissionId]/claim | mentor | /api/v1/mentor/submissions/{submissionId}/claim | none or empty body | { success: true } or submission detail with claimed=true |
| M | POST | /api/mentor/submissions/[submissionId]/decide | mentor | /api/v1/mentor/submissions/{submissionId}/decide | { kind(accept/rework/reject/withdrawn/reassigned), reviewerC | { success: true, decision: { reviewId, kind, ... } } |
| M | POST | /api/mentor/submissions/[submissionId]/release | mentor | /api/v1/mentor/submissions/{submissionId}/release | none or empty body | { success: true } or submission detail with claimed=false |
| L | POST,PUT | /api/mentor/reviews/[reviewId] | mentor | LOCAL | POST: { kind(accept/rework/reject/withdrawn/reassigned), rev | POST: { success: true, decision: object }. PUT: { success: t |
| P | GET | /api/mentor/contributors/[contributorId]/notes | mentor | PRISMA | none (contributorId from path param) | { items: Array<{ id, body, attachments, created_at, visibili |
| L | POST | /api/mentors/invites | super-admin | LOCAL | { email(string), firstName?(string), lastName?(string), name | { code(string), registerUrl(string), email(string), expiresA |
| L | POST | /api/mentors/invites/validate | super-admin | LOCAL | { inviteCode(string), email(string) } | { ok: true, orgLabel?: string } / { message: string } on err |
| E | POST | /api/reviewer/create | super-admin | /api/v1/users | email, firstName, lastName, role, phone, department, tenantI | id, email, firstName, lastName, name, role, phone, departmen |
| E | GET,POST | /api/reviewers/invitations | super-admin | /api/v1/users (POST) / /api/v1/users/reviewers/list (GET) | GET: none; POST: email, firstName, lastName, role, phone, de | GET: {reviewers: [...user objects], total}; POST: {id, email |
| L | POST | /api/reviewers/invites | frontend-local | LOCAL | email, note (optional), max 2000 chars | code, registerUrl, email, expiresAt, emailSent |
| L | POST | /api/reviewers/invites/validate | frontend-local | LOCAL | inviteCode, email | ok, orgName, expiresAt |
| P | POST | /api/admin/agents/[agentId]/prompts/[templateName]/rollback | super-admin | PRISMA | targetVersionId: string, reason?: string | result: {toPromptVersionId, toVersion, fromPromptVersionId,  |
| P | GET | /api/admin/agents/[agentId]/prompts/[templateName]/versions | super-admin | PRISMA | none | versions: array of PromptVersion objects |
| L | POST | /api/admin/audit/export | super-admin | LOCAL | emailTo: string, format?: string, window?: string, tenant?:  | success: boolean, requestId: string, message: string |
| L | GET,PUT | /api/admin/email-settings/smtp | super-admin | LOCAL | PUT: {host, port, user, password} / GET: none | GET: {config: {...}} / PUT: {success: boolean, config: {...} |
| L | POST | /api/admin/email-settings/smtp/test | super-admin | LOCAL | host: string, port: number, user: string, password: string | success: boolean, message: string |
| P | POST | /api/admin/tenants/[tenantId]/sso | super-admin | PRISMA | TenantSsoConfig (kind, enabled, provider-specific config) | ok: boolean, tenantId: string, kind: string, enabled: boolea |
| P | GET | /api/admin/tenants/[tenantId]/subscription/history | super-admin | PRISMA | none | tenantId: string, items: array of subscription history |
| P | GET,PATCH,POST | /api/admin/tenants/[tenantId]/subscription | super-admin | PRISMA | GET: none / PATCH: {planCode, contractRef?, trialDays?} / PO | tenantId, plan, availablePlans, usageCounters, source |
| M | GET | /api/matching/tasks/[taskId]/candidates | super-admin | /api/v1/matching/tasks/[taskId]/candidates | query parameters (passed through to backend) | candidates: array from backend |
| E | GET | /api/config/contributor-pricing | super-admin | /api/v1/config/contributor-pricing | none (public) | data: {student: {currency, hourlyRate}, workforceSlabs: [... |
| E | GET,PUT | /api/settings/contributor-pricing | super-admin | /api/v1/settings/contributor-pricing | GET: none / PUT: {student, workforceSlabs} | data: {student: {currency, hourlyRate}, workforceSlabs: [... |
| E | POST | /api/superadmin/users | super-admin | /api/v1/superadmin/users | email, firstName?, lastName?, name?, role?, phone?, departme | user object: {id, email, name, role, createdAt, ...} |
| M | POST | POST /api/ai/invoke | super-admin | REQUIRES_NEW_ENDPOINT | agentId (enum: sow-intake/decomposition/contributor-support/ | { ok: boolean, response?: object, failure?: object } |
| M | GET | GET /api/audit/export | super-admin | /api/superadmin/audit-log/export | format (csv/json/ndjson default json), tenantId, actionPrefi | CSV/JSON/NDJSON body with headers X-Audit-Row-Count, X-Audit |
| M | POST | POST /api/email/send | super-admin | REQUIRES_NEW_ENDPOINT | event (enum: sow_stage_activated/sow_stage_approved/sow_chan | { success: boolean, error?: string } |
| P | GET | GET /api/file-scan/artifacts/[artifactId] | super-admin | PRISMA_LOCAL_LOGIC | artifactId (path parameter) | { scanCleared: boolean, scanAttemptedAt: ISO8601 / null, sca |
| P | POST | POST /api/file-scan/process | super-admin | PRISMA_LOCAL_LOGIC | limit? (number 1-500, default 25) | { processed: number, results: array } |
| L | GET | GET /api/nda-document | freelancer | LOCAL | none (reads static docx from public/) | { html: string (converted from docx) } |
| L | GET | GET /api/nda-download | freelancer | LOCAL | name? (query), date? (query, ISO date format) | PDF binary stream with name+date embedded |
| E | GET | GET /api/public/credentials/[shareId] | freelancer | /api/public/credentials/{shareId} | shareId (path parameter) | { share_id, title, issuer, kind, status, verification_code,  |
| M | POST | POST /api/razorpay/create-order | enterprise | /api/v1/razorpay/create-order | proxied to backend (body forwarded as-is) | proxied backend response |
| M | POST | POST /api/razorpay/webhook | enterprise | /api/v1/razorpay/webhook | proxied to backend (body forwarded as-is) | proxied backend response |
| M | POST | POST /api/razorpay/payout-webhook | enterprise | /api/v1/razorpay/payout-webhook | proxied to backend (body forwarded as-is) | proxied backend response |
| P | GET | GET /api/breadcrumb/label | super-admin | PRISMA_LOCAL_LOGIC | type (query: enum 'plan'), id (query: uuid) | { type, id, label: string } |
---
## Build complete (2026-06-10)
All 86 missing/prisma-only endpoints built in `backends/` (canonical):
- enterprise: 31 (sow-actions, decomposition plans, review-queue, tasks-assign+workforce, compliance+ratecards+billing-export+razorpay-simulated)
- freelancer: 31 (submissions, payouts-simulated, notifications, contributor tasks/track/account-auth/mentorship/credentials-wallet)
- super-admin: 17 (sso/oidc/saml, reviewer register, tenant subscription+sso, ai/agents+matching, audit-export/email/file-scan/breadcrumb)
- mentor+auth: 20 (mentor me/queue/submissions claim·decide·release, notes, sessions, mentor register, user login-sessions)

Persistence: real Neon Postgres (idempotent CREATE TABLE on startup). Audit + file-scan events: MongoDB. Payments (payouts/razorpay): simulated.
Gateway (backends/gateway.py) routing extended: /api/v1/submissions·payouts·notifications·razorpay·mentor·sessions, /api/ai·audit·email·file-scan·breadcrumb·matching.
All 5 backends + gateway boot clean; new endpoints verified responding via gateway with real role tokens.
