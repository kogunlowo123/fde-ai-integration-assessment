# Account Security

An account is suspended automatically after ten consecutive failed sign-in
attempts. The suspension lasts for one hour, after which the account is
reinstated automatically.

Multi-factor authentication is required for every administrative role.
Administrators who have not enrolled a second factor lose administrative
privileges after thirty days.

API keys are rotated every ninety days. A key believed to be compromised must
be rotated immediately by an administrator; rotation invalidates the previous
key at once and cannot be undone.

Support agents can never see a customer's password or full payment card
number. Only the last four digits of a card are visible in the support tool.
