"""Auth forms using Flask-WTF."""

from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, BooleanField
from wtforms.validators import DataRequired, Length, Regexp, EqualTo


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])
    remember_me = BooleanField("Remember me")
    totp_code = StringField("Authenticator code")
    submit = SubmitField("Login")


class TwoFAForm(FlaskForm):
    totp_code = StringField("Authenticator code", validators=[DataRequired(), Regexp(r"^\d{6}$", message="Enter the 6-digit code")])
    submit = SubmitField("Confirm 2FA")


password_policy_validators = [
    Length(min=12, message="Password must be at least 12 characters."),
    Regexp(r".*[A-Z].*", message="Password must include an uppercase letter."),
    Regexp(r".*[a-z].*", message="Password must include a lowercase letter."),
    Regexp(r".*\d.*", message="Password must include a digit."),
    Regexp(r".*[^A-Za-z0-9].*", message="Password must include a special character."),
]


class PasswordResetForm(FlaskForm):
    password = PasswordField("New password", validators=[DataRequired()] + password_policy_validators)
    confirm = PasswordField("Confirm password", validators=[DataRequired(), EqualTo('password', message='Passwords must match')])
    submit = SubmitField("Set Password")
