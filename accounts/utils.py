"""
Utility functions for user code and password generation.
"""

import random
import string
import threading
from datetime import datetime
from django.conf import settings
from django.template.loader import render_to_string
from .models import User

# Thread-local storage for the current tenant database
_thread_locals = threading.local()

def set_current_db(db_name):
    """Set the database name for the current thread"""
    _thread_locals.current_db = db_name

def get_current_db():
    """Get the database name for the current thread"""
    return getattr(_thread_locals, 'current_db', 'default')

def clear_current_db():
    """Clear the database name for the current thread"""
    if hasattr(_thread_locals, 'current_db'):
        del _thread_locals.current_db


def generate_user_code(role: str, center_code: str = None, batch_code: str = None) -> str:
    """
    Generate a unique user code based on role and context.
    
    Format:
    - ADMIN: ADM-<center_code>-<3_digits>
    - TEACHER: TCH-<center_code>-<3_digits> or TCH-<3_digits>
    - STUDENT: STU-<batch_code>-<4_digits> or STU-<4_digits>
    
    Args:
        role: User role (ADMIN, TEACHER, STUDENT, or exam roles)
        center_code: Optional center code (first 3-4 chars)
        batch_code: Optional batch code (for students)
    
    Returns:
        Unique user code string
    """
    # Map exam roles to timetable roles for code generation
    role_mapping = {
        'super_admin': 'ADMIN',
        'SUPER_ADMIN': 'ADMIN',
        'institute_admin': 'ADMIN',
        'exam_admin': 'ADMIN',
        'admin': 'ADMIN',
        'ADMIN': 'ADMIN',
        'teacher': 'TEACHER',
        'TEACHER': 'TEACHER',
        'student': 'STUDENT',
        'STUDENT': 'STUDENT',
        'staff': 'STAFF',
        'STAFF': 'STAFF',
    }
    
    mapped_role = role_mapping.get(role, role)
    
    # Get current year for reference
    current_year = datetime.now().year
    
    if mapped_role == 'ADMIN':
        prefix = "ADM"
        if center_code:
            center_part = center_code[:4].upper().replace("-", "")
            suffix = f"{random.randint(100, 999)}"
            code = f"{prefix}-{center_part}-{suffix}"
        else:
            code = f"{prefix}-{random.randint(1000, 9999)}"
    
    elif mapped_role == 'TEACHER':
        prefix = "TCH"
        if center_code:
            center_part = center_code[:4].upper().replace("-", "")
            suffix = f"{random.randint(100, 999)}"
            code = f"{prefix}-{center_part}-{suffix}"
        else:
            code = f"{prefix}-{random.randint(1000, 9999)}"
    
    elif mapped_role == 'STUDENT':
        prefix = "STU"
        if batch_code:
            batch_part = batch_code[:6].upper().replace("-", "").replace(" ", "")
            suffix = f"{random.randint(1000, 9999)}"
            code = f"{prefix}-{batch_part}-{suffix}"
        else:
            code = f"{prefix}-{random.randint(10000, 99999)}"
    
    elif mapped_role == 'STAFF':
        prefix = "STF"
        if center_code:
            center_part = center_code[:4].upper().replace("-", "")
            suffix = f"{random.randint(100, 999)}"
            code = f"{prefix}-{center_part}-{suffix}"
        else:
            code = f"{prefix}-{random.randint(1000, 9999)}"
    
    else:
        # Fallback for other roles
        prefix = mapped_role[:3].upper()
        code = f"{prefix}-{random.randint(1000, 9999)}"
    
    # Ensure uniqueness
    base_code = code
    counter = 1
    while User.objects.filter(username=code).exists():
        if "-" in code:
            parts = code.rsplit("-", 1)
            code = f"{parts[0]}-{counter:03d}"
        else:
            code = f"{base_code}{counter:03d}"
        counter += 1
    
    return code


def generate_password(role: str, center_code: str = None, batch_code: str = None,
                      date_of_birth: str = None, year: int = None,
                      name: str = None, phone_number: str = None) -> str:
    """
    Generate a password based on role and context.

    Password format:
    - ADMIN: Admin@<center_code><current_year>
    - TEACHER: Teacher@<center_code><current_year>
    - STUDENT: <Name>@<last_3_digits_of_phone> (first and last letters capitalized)
      Falls back to Student@<batch_code><year> if name or phone not available
    - STAFF: Staff@<center_code><current_year>

    Args:
        role: User role
        center_code: Optional center code
        batch_code: Optional batch code (for students)
        date_of_birth: Optional DOB in YYYY-MM-DD format
        year: Optional year (for students, could be batch year or DOB year)
        name: Optional student name (for student password generation)
        phone_number: Optional phone number (for student password generation)

    Returns:
        Generated password string
    """
    # Map exam roles to timetable roles for password generation
    role_mapping = {
        'super_admin': 'ADMIN',
        'SUPER_ADMIN': 'ADMIN',
        'institute_admin': 'ADMIN',
        'exam_admin': 'ADMIN',
        'admin': 'ADMIN',
        'ADMIN': 'ADMIN',
        'teacher': 'TEACHER',
        'TEACHER': 'TEACHER',
        'student': 'STUDENT',
        'STUDENT': 'STUDENT',
        'staff': 'STAFF',
        'STAFF': 'STAFF',
    }

    mapped_role = role_mapping.get(role, role)
    current_year = datetime.now().year

    if mapped_role == 'ADMIN':
        if center_code:
            center_part = center_code[:4].upper().replace("-", "").replace(" ", "")
            password = f"Admin@{center_part}{current_year}"
        else:
            password = f"Admin@{current_year}"

    elif mapped_role == 'TEACHER':
        # Create unique password using multiple factors
        password_parts = []
        password_parts.append("Teacher@")
        
        # Add center code if available
        if center_code:
            center_part = center_code[:4].upper().replace("-", "").replace(" ", "")
            password = f"Teacher@{center_part}{current_year}"
        else:
            password = f"Teacher@{current_year}"

    elif mapped_role == 'STUDENT':
        # New format: Name (first & last letter capitalized) + @ + last 3 digits of phone
        if name and phone_number and len(phone_number) >= 3:
            # Use first name only (first word), capitalize first and last letters
            first_name = name.strip().split()[0] if name.strip() else name.strip()
            if len(first_name) >= 2:
                formatted_name = first_name[0].upper() + first_name[1:-1].lower() + first_name[-1].upper()
            elif len(first_name) == 1:
                formatted_name = first_name.upper()
            else:
                formatted_name = first_name
            last_3_digits = phone_number.strip()[-3:]
            password = f"{formatted_name}@{last_3_digits}"
        else:
            # Fallback to old format if name or phone not available
            if batch_code:
                import re
                year_match = re.search(r'\d{4}', batch_code)
                if year_match:
                    year = int(year_match.group())
                else:
                    year = current_year
            elif date_of_birth:
                try:
                    dob_date = datetime.strptime(date_of_birth, "%Y-%m-%d")
                    year = dob_date.year
                except:
                    year = current_year
            elif not year:
                year = current_year

            if batch_code:
                batch_part = batch_code[:6].upper().replace("-", "").replace(" ", "")
                password = f"Student@{batch_part}{year}"
            else:
                password = f"Student@{year}"

    elif mapped_role == 'STAFF':
        # Create unique password using multiple factors
        password_parts = []
        password_parts.append("Staff@")
        
        # Add center code if available
        if center_code:
            center_part = center_code[:4].upper().replace("-", "").replace(" ", "")
            password = f"Staff@{center_part}{current_year}"
        else:
            password = f"Staff@{current_year}"

    else:
        password = f"User@{current_year}"

    return password


def log_activity(institute, log_type, title, description, user=None, status='info', metadata=None, request=None):
    """
    Utility function to create an activity log entry.
    """
    try:
        from .models import ActivityLog
        
        ip_address = None
        if request:
            x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
            if x_forwarded_for:
                ip_address = x_forwarded_for.split(',')[0]
            else:
                ip_address = request.META.get('REMOTE_ADDR')

        return ActivityLog.objects.create(
            institute=institute,
            log_type=log_type,
            title=title,
            description=description,
            user=user,
            status=status,
            metadata=metadata or {},
            ip_address=ip_address
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Failed to log activity: {e}")
        return None


def send_credentials_email(user, password):
    """
    Send an email to the user with their account credentials using Mailgun.
    Only sends when DEBUG is False (production).
    """
    try:
        if True or settings.DEBUG:
            print(f"DEBUG mode: Skipping credential email for {user.username}.")
            return False

        if not user.email:
            print(f"User {user.username} has no email address. Skipping credential email.")
            return False
        
        import requests

        context = {
            'name': user.get_full_name() or user.username,
            'role': user.role,
            'username': user.username,
            'password': password,
            'institute_name': user.institute.name if user.institute else "Exam Flow System",
            'center_name': user.center.name if user.center else None,
            'login_url': f"{settings.FRONTEND_URL}/login",
        }

        subject = "Your Account Credentials - Exam Dasho App"
        html_content = render_to_string('emails/credential_notification.html', context)

        response = requests.post(
            f"https://api.mailgun.net/v3/{settings.MAILGUN_DOMAIN}/messages",
            auth=("api", settings.MAILGUN_API_KEY),
            data={
                "from": settings.DEFAULT_FROM_EMAIL,
                "to": user.email,
                "subject": subject,
                "html": html_content,
            },
        )

        if response.status_code == 200:
            print(f"Credential email sent to {user.email} via Mailgun (id: {response.json().get('id', 'N/A')})")
            return True
        else:
            print(f"Mailgun error for {user.email}: {response.status_code} - {response.text}")
            return False

    except Exception as e:
        import traceback
        print(f"Failed to send credential email: {str(e)}")
        traceback.print_exc()
        return False
