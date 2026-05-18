from datetime import date, datetime, timedelta
from functools import wraps

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from models import Appointment, Doctor, Patient, Schedule, User, db

bp = Blueprint("main", __name__)

ROLE_LABELS = {
    "patient": "Пациент",
    "doctor": "Врач",
    "admin": "Администратор",
}

STATUS_LABELS = {
    "confirmed": "Подтверждена",
    "cancelled": "Отменена",
    "rescheduled": "Перенесена",
}


@bp.before_app_request
def load_logged_in_user():
    user_id = session.get("user_id")
    g.current_user = db.session.get(User, user_id) if user_id else None


@bp.app_context_processor
def inject_globals():
    return {
        "current_user": g.get("current_user"),
        "role_labels": ROLE_LABELS,
        "status_labels": STATUS_LABELS,
        "today_date": date.today(),
    }


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not g.current_user or not g.current_user.is_active:
            session.clear()
            flash("Войдите в систему, чтобы продолжить работу.", "warning")
            return redirect(url_for("main.login"))
        return view(*args, **kwargs)

    return wrapped_view


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped_view(*args, **kwargs):
            if g.current_user.role not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped_view

    return decorator


def parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        return None


def combine_date_time(day, time_value):
    return datetime.combine(day, time_value)


def get_active_appointments_for_doctor(doctor_id, appointment_date, exclude_appointment_id=None):
    query = (
        Appointment.query.filter_by(doctor_id=doctor_id, appointment_date=appointment_date)
        .filter(Appointment.status != "cancelled")
        .order_by(Appointment.start_time)
    )
    if exclude_appointment_id:
        query = query.filter(Appointment.id != exclude_appointment_id)
    return query.all()


def slot_overlaps(slot_start, slot_end, appointment):
    return slot_start < appointment.end_time and slot_end > appointment.start_time


def generate_slots_for_schedule(schedule):
    current_dt = combine_date_time(schedule.work_date, schedule.start_time)
    end_dt = combine_date_time(schedule.work_date, schedule.end_time)
    step = timedelta(minutes=schedule.slot_duration)
    slots = []

    while current_dt + step <= end_dt:
        slots.append(
            {
                "schedule_id": schedule.id,
                "date": schedule.work_date,
                "schedule_start": schedule.start_time,
                "schedule_end": schedule.end_time,
                "start_time": current_dt.time(),
                "end_time": (current_dt + step).time(),
                "duration": schedule.slot_duration,
            }
        )
        current_dt += step

    return slots


def calculate_efficiency_score(slot, existing_appointments):
    slot_start_dt = combine_date_time(slot["date"], slot["start_time"])
    slot_end_dt = combine_date_time(slot["date"], slot["end_time"])
    prev_anchor = combine_date_time(slot["date"], slot["schedule_start"])
    next_anchor = combine_date_time(slot["date"], slot["schedule_end"])

    for appointment in existing_appointments:
        appointment_end = combine_date_time(slot["date"], appointment.end_time)
        if appointment_end <= slot_start_dt:
            prev_anchor = appointment_end
        else:
            break

    for appointment in existing_appointments:
        appointment_start = combine_date_time(slot["date"], appointment.start_time)
        if appointment_start >= slot_end_dt:
            next_anchor = appointment_start
            break

    gap_before = int((slot_start_dt - prev_anchor).total_seconds() // 60)
    gap_after = int((next_anchor - slot_end_dt).total_seconds() // 60)
    adjacency_bonus = 0
    if gap_before == 0:
        adjacency_bonus -= 10
    if gap_after == 0:
        adjacency_bonus -= 10

    return gap_before + gap_after + adjacency_bonus


def collect_available_slots(doctor, appointment_date, exclude_appointment_id=None):
    schedules = (
        Schedule.query.filter_by(
            doctor_id=doctor.id,
            work_date=appointment_date,
            is_active=True,
        )
        .order_by(Schedule.start_time)
        .all()
    )
    existing_appointments = get_active_appointments_for_doctor(
        doctor.id,
        appointment_date,
        exclude_appointment_id=exclude_appointment_id,
    )

    available_slots = []
    for schedule in schedules:
        for slot in generate_slots_for_schedule(schedule):
            if any(slot_overlaps(slot["start_time"], slot["end_time"], appointment) for appointment in existing_appointments):
                continue
            slot["efficiency_score"] = calculate_efficiency_score(slot, existing_appointments)
            available_slots.append(slot)

    available_slots.sort(key=lambda item: item["start_time"])
    recommended_slot = min(
        available_slots,
        key=lambda item: (item["efficiency_score"], item["start_time"]),
        default=None,
    )
    return available_slots, recommended_slot


def find_exact_available_slot(doctor, appointment_date, start_time, exclude_appointment_id=None):
    available_slots, _ = collect_available_slots(
        doctor,
        appointment_date,
        exclude_appointment_id=exclude_appointment_id,
    )
    for slot in available_slots:
        if slot["start_time"] == start_time:
            return slot
    return None


def find_nearest_available_slot(doctor, target_date, target_time, exclude_appointment_id=None, search_days=14):
    for offset in range(search_days + 1):
        candidate_date = target_date + timedelta(days=offset)
        available_slots, _ = collect_available_slots(
            doctor,
            candidate_date,
            exclude_appointment_id=exclude_appointment_id,
        )
        for slot in available_slots:
            if offset == 0 and target_time and slot["start_time"] < target_time:
                continue
            return slot
    return None


def find_patient_overlap(patient_id, appointment_date, start_time, end_time, exclude_appointment_id=None):
    query = (
        Appointment.query.filter_by(patient_id=patient_id, appointment_date=appointment_date)
        .filter(Appointment.status != "cancelled")
        .order_by(Appointment.start_time)
    )
    if exclude_appointment_id:
        query = query.filter(Appointment.id != exclude_appointment_id)

    for appointment in query.all():
        if start_time < appointment.end_time and end_time > appointment.start_time:
            return appointment
    return None


def format_slot_for_message(slot):
    if not slot:
        return ""
    return f"{slot['date'].strftime('%d.%m.%Y')} в {slot['start_time'].strftime('%H:%M')}"


def get_dashboard_metrics():
    today = date.today()
    upcoming_appointments = Appointment.query.filter(
        Appointment.appointment_date >= today,
        Appointment.status != "cancelled",
    ).count()

    schedules_next_week = Schedule.query.filter(
        Schedule.work_date.between(today, today + timedelta(days=7)),
        Schedule.is_active.is_(True),
    ).all()
    total_slots = sum(schedule.total_slots for schedule in schedules_next_week)
    booked_slots = Appointment.query.filter(
        Appointment.appointment_date.between(today, today + timedelta(days=7)),
        Appointment.status != "cancelled",
    ).count()
    clinic_load = round((booked_slots / total_slots) * 100, 1) if total_slots else 0

    return {
        "doctor_count": Doctor.query.count(),
        "patient_count": Patient.query.count(),
        "upcoming_appointments": upcoming_appointments,
        "clinic_load": clinic_load,
    }


def build_load_report(window_days=7):
    start_date = date.today()
    end_date = start_date + timedelta(days=window_days)
    doctors = Doctor.query.options(joinedload(Doctor.user)).order_by(Doctor.specialization).all()
    report = []

    for doctor in doctors:
        schedules = Schedule.query.filter(
            Schedule.doctor_id == doctor.id,
            Schedule.work_date.between(start_date, end_date),
            Schedule.is_active.is_(True),
        ).all()
        total_slots = sum(schedule.total_slots for schedule in schedules)
        booked_slots = Appointment.query.filter(
            Appointment.doctor_id == doctor.id,
            Appointment.appointment_date.between(start_date, end_date),
            Appointment.status != "cancelled",
        ).count()
        load_percent = round((booked_slots / total_slots) * 100, 1) if total_slots else 0
        report.append(
            {
                "doctor": doctor,
                "total_slots": total_slots,
                "booked_slots": booked_slots,
                "load_percent": load_percent,
            }
        )

    return sorted(report, key=lambda item: item["load_percent"], reverse=True)


def get_next_appointment_for_user(user):
    today = date.today()
    query = (
        Appointment.query.options(
            joinedload(Appointment.doctor).joinedload(Doctor.user),
            joinedload(Appointment.patient).joinedload(Patient.user),
        )
        .filter(Appointment.appointment_date >= today, Appointment.status != "cancelled")
        .order_by(Appointment.appointment_date, Appointment.start_time)
    )

    if not user:
        return None
    if user.role == "patient" and user.patient_profile:
        query = query.filter_by(patient_id=user.patient_profile.id)
    elif user.role == "doctor" and user.doctor_profile:
        query = query.filter_by(doctor_id=user.doctor_profile.id)
    else:
        return None

    return query.first()


@bp.route("/")
def index():
    doctors = (
        Doctor.query.options(joinedload(Doctor.user))
        .join(Doctor.user)
        .filter(User.is_active.is_(True))
        .order_by(Doctor.specialization, User.full_name)
        .all()
    )
    metrics = get_dashboard_metrics()
    next_appointment = get_next_appointment_for_user(g.get("current_user"))
    return render_template(
        "index.html",
        doctors=doctors,
        metrics=metrics,
        next_appointment=next_appointment,
    )


@bp.route("/register", methods=["GET", "POST"])
def register():
    can_create_admin = bool(g.current_user and g.current_user.role == "admin") or User.query.filter_by(role="admin").count() == 0

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip() or None
        role = request.form.get("role", "patient")
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        specialization = request.form.get("specialization", "").strip()
        room_number = request.form.get("room_number", "").strip() or None
        appointment_duration = request.form.get("appointment_duration", type=int) or 30
        birth_date = parse_date(request.form.get("birth_date"))
        insurance_number = request.form.get("insurance_number", "").strip() or None

        if role == "admin" and not can_create_admin:
            flash("Создание новых администраторов доступно только действующему администратору.", "danger")
        elif not full_name or not email or not password:
            flash("Заполните имя, email и пароль.", "danger")
        elif password != confirm_password:
            flash("Пароли не совпадают.", "danger")
        elif len(password) < 6:
            flash("Пароль должен содержать минимум 6 символов.", "danger")
        elif User.query.filter(func.lower(User.email) == email).first():
            flash("Пользователь с таким email уже существует.", "danger")
        elif phone and User.query.filter_by(phone=phone).first():
            flash("Пользователь с таким номером телефона уже зарегистрирован.", "danger")
        elif role == "doctor" and not specialization:
            flash("Для врача нужно указать специализацию.", "danger")
        elif role == "doctor" and not 10 <= appointment_duration <= 120:
            flash("Длительность приёма должна быть от 10 до 120 минут.", "danger")
        elif request.form.get("birth_date") and not birth_date:
            flash("Укажите корректную дату рождения.", "danger")
        else:
            user = User(
                full_name=full_name,
                email=email,
                phone=phone,
                role=role,
            )
            user.set_password(password)
            db.session.add(user)

            if role == "doctor":
                db.session.add(
                    Doctor(
                        user=user,
                        specialization=specialization,
                        room_number=room_number,
                        appointment_duration=appointment_duration,
                    )
                )
            elif role == "patient":
                db.session.add(
                    Patient(
                        user=user,
                        birth_date=birth_date,
                        insurance_number=insurance_number,
                    )
                )

            db.session.commit()
            flash("Регистрация прошла успешно.", "success")

            if g.current_user and g.current_user.role == "admin":
                return redirect(url_for("main.admin_dashboard"))

            session.clear()
            session["user_id"] = user.id
            if user.role == "doctor":
                return redirect(url_for("main.schedule_view"))
            if user.role == "admin":
                return redirect(url_for("main.admin_dashboard"))
            return redirect(url_for("main.appointments"))

    return render_template("register.html", can_create_admin=can_create_admin)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter(func.lower(User.email) == email).first()

        if not user or not user.check_password(password):
            flash("Неверный email или пароль.", "danger")
        elif not user.is_active:
            flash("Учетная запись деактивирована администратором.", "danger")
        else:
            session.clear()
            session["user_id"] = user.id
            flash("Вы успешно вошли в систему.", "success")
            if user.role == "admin":
                return redirect(url_for("main.admin_dashboard"))
            if user.role == "doctor":
                return redirect(url_for("main.schedule_view"))
            return redirect(url_for("main.appointments"))

    return render_template("login.html")


@bp.route("/logout")
def logout():
    session.clear()
    flash("Вы вышли из системы.", "info")
    return redirect(url_for("main.index"))


@bp.route("/appointments", methods=["GET", "POST"])
@login_required
def appointments():
    doctors = (
        Doctor.query.options(joinedload(Doctor.user))
        .join(Doctor.user)
        .filter(User.is_active.is_(True))
        .order_by(Doctor.specialization, User.full_name)
        .all()
    )
    booking_patients = (
        Patient.query.options(joinedload(Patient.user))
        .join(Patient.user)
        .filter(User.is_active.is_(True))
        .order_by(User.full_name)
        .all()
        if g.current_user.role == "admin"
        else []
    )

    selected_doctor = None
    selected_doctor_id = request.values.get("doctor_id", type=int)
    selected_date = parse_date(request.values.get("appointment_date")) or date.today()
    if g.current_user.role == "doctor" and g.current_user.doctor_profile:
        selected_doctor = g.current_user.doctor_profile
        selected_doctor_id = selected_doctor.id
    elif doctors:
        selected_doctor_id = selected_doctor_id or doctors[0].id
        selected_doctor = db.session.get(Doctor, selected_doctor_id)

    if request.method == "POST" and g.current_user.role in ("patient", "admin"):
        doctor = db.session.get(Doctor, request.form.get("doctor_id", type=int))
        appointment_date = parse_date(request.form.get("appointment_date"))
        start_time = parse_time(request.form.get("start_time"))
        reason = request.form.get("reason", "").strip() or None
        patient_id = (
            request.form.get("patient_id", type=int)
            if g.current_user.role == "admin"
            else (g.current_user.patient_profile.id if g.current_user.patient_profile else None)
        )

        if not doctor or not appointment_date or not start_time:
            flash("Выберите врача, дату и время приёма.", "danger")
        elif appointment_date < date.today():
            flash("Нельзя записаться на прошедшую дату.", "danger")
        elif not patient_id:
            flash("Для записи нужен профиль пациента.", "danger")
        else:
            selected_doctor = doctor
            selected_doctor_id = doctor.id
            selected_date = appointment_date
            slot = find_exact_available_slot(doctor, appointment_date, start_time)

            if not slot:
                suggestion = find_nearest_available_slot(doctor, appointment_date, start_time)
                if suggestion:
                    flash(
                        f"Выбранный слот уже занят. Ближайшее свободное время: {format_slot_for_message(suggestion)}.",
                        "warning",
                    )
                else:
                    flash("На выбранную дату свободных слотов нет.", "warning")
            else:
                overlapping_appointment = find_patient_overlap(
                    patient_id,
                    appointment_date,
                    slot["start_time"],
                    slot["end_time"],
                )
                if overlapping_appointment:
                    flash("У пациента уже есть другая запись на это время.", "danger")
                else:
                    appointment = Appointment(
                        patient_id=patient_id,
                        doctor_id=doctor.id,
                        schedule_id=slot["schedule_id"],
                        appointment_date=appointment_date,
                        start_time=slot["start_time"],
                        end_time=slot["end_time"],
                        duration_minutes=slot["duration"],
                        status="confirmed",
                        reason=reason,
                        notes="Подтверждение записи отправлено пользователю.",
                    )
                    db.session.add(appointment)
                    try:
                        db.session.commit()
                        flash(
                            f"Запись подтверждена на {appointment_date.strftime('%d.%m.%Y')} в {slot['start_time'].strftime('%H:%M')}.",
                            "success",
                        )
                        return redirect(
                            url_for(
                                "main.appointments",
                                doctor_id=doctor.id,
                                appointment_date=appointment_date.isoformat(),
                            )
                        )
                    except IntegrityError:
                        db.session.rollback()
                        flash("Слот только что заняли. Выберите другое время.", "danger")

    available_slots = []
    recommended_slot = None
    if selected_doctor and selected_date:
        available_slots, recommended_slot = collect_available_slots(selected_doctor, selected_date)

    appointment_query = Appointment.query.options(
        joinedload(Appointment.doctor).joinedload(Doctor.user),
        joinedload(Appointment.patient).joinedload(Patient.user),
    ).order_by(Appointment.appointment_date, Appointment.start_time)

    if g.current_user.role == "patient" and g.current_user.patient_profile:
        appointment_query = appointment_query.filter_by(patient_id=g.current_user.patient_profile.id)
    elif g.current_user.role == "doctor" and g.current_user.doctor_profile:
        appointment_query = appointment_query.filter_by(doctor_id=g.current_user.doctor_profile.id)

    user_appointments = appointment_query.all()

    return render_template(
        "appointments.html",
        doctors=doctors,
        booking_patients=booking_patients,
        appointments=user_appointments,
        selected_doctor=selected_doctor,
        selected_doctor_id=selected_doctor_id,
        selected_date=selected_date,
        available_slots=available_slots,
        recommended_slot=recommended_slot,
        can_book=g.current_user.role in ("patient", "admin"),
    )


@bp.post("/appointments/<int:appointment_id>/cancel")
@login_required
def cancel_appointment(appointment_id):
    appointment = Appointment.query.options(
        joinedload(Appointment.patient).joinedload(Patient.user),
    ).get_or_404(appointment_id)

    if g.current_user.role == "patient":
        if not g.current_user.patient_profile or appointment.patient_id != g.current_user.patient_profile.id:
            abort(403)
    elif g.current_user.role not in ("admin",):
        abort(403)

    if appointment.status == "cancelled":
        flash("Эта запись уже отменена.", "info")
    else:
        appointment.status = "cancelled"
        appointment.notes = "Запись отменена, слот снова доступен для бронирования."
        db.session.commit()
        flash("Запись отменена, пациенту отправлено уведомление.", "success")

    return redirect(request.referrer or url_for("main.appointments"))


@bp.post("/appointments/<int:appointment_id>/reschedule")
@roles_required("admin")
def reschedule_appointment(appointment_id):
    appointment = Appointment.query.get_or_404(appointment_id)
    if appointment.status == "cancelled":
        flash("Нельзя перенести уже отменённую запись.", "warning")
        return redirect(request.referrer or url_for("main.appointments"))

    new_date = parse_date(request.form.get("new_date"))
    new_time = parse_time(request.form.get("new_time"))

    if not new_date or not new_time:
        flash("Укажите новую дату и время.", "danger")
        return redirect(request.referrer or url_for("main.appointments"))

    slot = find_exact_available_slot(
        appointment.doctor,
        new_date,
        new_time,
        exclude_appointment_id=appointment.id,
    )

    if not slot:
        suggestion = find_nearest_available_slot(
            appointment.doctor,
            new_date,
            new_time,
            exclude_appointment_id=appointment.id,
        )
        if suggestion:
            flash(
                f"Выбранный слот недоступен. Ближайшее время для переноса: {format_slot_for_message(suggestion)}.",
                "warning",
            )
        else:
            flash("Для этого врача пока нет свободных слотов для переноса.", "warning")
        return redirect(request.referrer or url_for("main.appointments"))

    overlapping_appointment = find_patient_overlap(
        appointment.patient_id,
        new_date,
        slot["start_time"],
        slot["end_time"],
        exclude_appointment_id=appointment.id,
    )
    if overlapping_appointment:
        flash("У пациента уже есть другая запись на выбранное время.", "danger")
        return redirect(request.referrer or url_for("main.appointments"))

    appointment.schedule_id = slot["schedule_id"]
    appointment.appointment_date = new_date
    appointment.start_time = slot["start_time"]
    appointment.end_time = slot["end_time"]
    appointment.duration_minutes = slot["duration"]
    appointment.status = "rescheduled"
    appointment.notes = "Запись перенесена администратором, пользователю отправлено уведомление."

    try:
        db.session.commit()
        flash("Запись успешно перенесена.", "success")
    except IntegrityError:
        db.session.rollback()
        flash("Не удалось перенести запись: слот уже занят.", "danger")

    return redirect(request.referrer or url_for("main.appointments"))


@bp.route("/schedule", methods=["GET", "POST"])
@roles_required("doctor", "admin")
def schedule_view():
    doctors = (
        Doctor.query.options(joinedload(Doctor.user))
        .join(Doctor.user)
        .filter(User.is_active.is_(True))
        .order_by(User.full_name)
        .all()
    )

    if g.current_user.role == "doctor":
        selected_doctor = g.current_user.doctor_profile
    else:
        requested_doctor_id = request.values.get("doctor_id", type=int)
        selected_doctor = db.session.get(Doctor, requested_doctor_id) if requested_doctor_id else (doctors[0] if doctors else None)

    if request.method == "POST":
        doctor = selected_doctor
        if g.current_user.role == "admin":
            doctor = db.session.get(Doctor, request.form.get("doctor_id", type=int))

        work_date = parse_date(request.form.get("work_date"))
        start_time = parse_time(request.form.get("start_time"))
        end_time = parse_time(request.form.get("end_time"))
        slot_duration = request.form.get("slot_duration", type=int) or (doctor.appointment_duration if doctor else 30)
        notes = request.form.get("notes", "").strip() or None

        if not doctor or not work_date or not start_time or not end_time:
            flash("Заполните дату, время и выберите врача.", "danger")
        elif work_date < date.today():
            flash("Нельзя добавлять расписание на прошедшую дату.", "danger")
        elif start_time >= end_time:
            flash("Время окончания должно быть позже времени начала.", "danger")
        elif not 10 <= slot_duration <= 120:
            flash("Длительность слота должна быть от 10 до 120 минут.", "danger")
        else:
            schedule = Schedule(
                doctor_id=doctor.id,
                work_date=work_date,
                start_time=start_time,
                end_time=end_time,
                slot_duration=slot_duration,
                notes=notes,
            )
            db.session.add(schedule)
            try:
                db.session.commit()
                flash("График врача обновлён.", "success")
                if g.current_user.role == "admin":
                    return redirect(url_for("main.schedule_view", doctor_id=doctor.id))
                return redirect(url_for("main.schedule_view"))
            except IntegrityError:
                db.session.rollback()
                flash("Такой рабочий интервал уже существует.", "danger")

    schedules = []
    upcoming_appointments = []
    if selected_doctor:
        schedules = (
            Schedule.query.filter(
                Schedule.doctor_id == selected_doctor.id,
                Schedule.work_date >= date.today(),
            )
            .order_by(Schedule.work_date, Schedule.start_time)
            .all()
        )
        upcoming_appointments = (
            Appointment.query.options(joinedload(Appointment.patient).joinedload(Patient.user))
            .filter(
                Appointment.doctor_id == selected_doctor.id,
                Appointment.appointment_date >= date.today(),
                Appointment.status != "cancelled",
            )
            .order_by(Appointment.appointment_date, Appointment.start_time)
            .all()
        )

    return render_template(
        "schedule.html",
        doctors=doctors,
        selected_doctor=selected_doctor,
        schedules=schedules,
        upcoming_appointments=upcoming_appointments,
    )


@bp.route("/admin")
@roles_required("admin")
def admin_dashboard():
    doctors = (
        Doctor.query.options(joinedload(Doctor.user))
        .join(Doctor.user)
        .order_by(User.full_name)
        .all()
    )
    patients = (
        Patient.query.options(joinedload(Patient.user))
        .join(Patient.user)
        .order_by(User.full_name)
        .all()
    )
    appointments = (
        Appointment.query.options(
            joinedload(Appointment.doctor).joinedload(Doctor.user),
            joinedload(Appointment.patient).joinedload(Patient.user),
        )
        .order_by(Appointment.appointment_date, Appointment.start_time)
        .all()
    )
    return render_template(
        "admin.html",
        metrics=get_dashboard_metrics(),
        load_report=build_load_report(),
        doctors=doctors,
        patients=patients,
        appointments=appointments,
    )


@bp.post("/admin/doctors/<int:doctor_id>/update")
@roles_required("admin")
def update_doctor(doctor_id):
    doctor = Doctor.query.options(joinedload(Doctor.user)).get_or_404(doctor_id)
    specialization = request.form.get("specialization", "").strip()
    room_number = request.form.get("room_number", "").strip() or None
    appointment_duration = request.form.get("appointment_duration", type=int) or doctor.appointment_duration

    if not specialization:
        flash("Укажите специализацию врача.", "danger")
    elif not 10 <= appointment_duration <= 120:
        flash("Длительность приёма должна быть от 10 до 120 минут.", "danger")
    else:
        doctor.specialization = specialization
        doctor.room_number = room_number
        doctor.appointment_duration = appointment_duration
        db.session.commit()
        flash(f"Данные врача {doctor.full_name} обновлены.", "success")

    return redirect(url_for("main.admin_dashboard"))


@bp.post("/admin/users/<int:user_id>/toggle")
@roles_required("admin")
def toggle_user_active(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == g.current_user.id:
        flash("Нельзя деактивировать собственную учётную запись.", "warning")
        return redirect(url_for("main.admin_dashboard"))

    user.is_active = not user.is_active
    db.session.commit()
    state = "активирован" if user.is_active else "деактивирован"
    flash(f"Пользователь {user.full_name} {state}.", "success")
    return redirect(url_for("main.admin_dashboard"))
