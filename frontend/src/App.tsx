/** The route tree.
 *
 * The three portals are separated at the routing layer rather than by convention, so the role
 * a screen belongs to is visible here in one place instead of being buried in a check at the
 * top of each component — and adding a screen to the wrong portal is a change you can see.
 */

import type { ReactNode } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'

import { AuthProvider } from './auth/AuthProvider'
import { HomeRedirect, RedirectIfSignedIn, RequireRole } from './auth/guards'
import { PortalShell } from './components/PortalShell'
import { CalendarSettingsPage } from './pages/CalendarSettingsPage'
import { LoginPage } from './pages/LoginPage'
import { NotFoundPage } from './pages/NotFoundPage'
import { RegisterPage } from './pages/RegisterPage'
import { CalendarSyncPage } from './pages/admin/CalendarSyncPage'
import { DoctorDetailPage } from './pages/admin/DoctorDetailPage'
import { DoctorsPage } from './pages/admin/DoctorsPage'
import { NewDoctorPage } from './pages/admin/NewDoctorPage'
import { NotificationsPage } from './pages/admin/NotificationsPage'
import { SchedulePage } from './pages/doctor/SchedulePage'
import { VisitPage } from './pages/doctor/VisitPage'
import { AppointmentDetailPage } from './pages/patient/AppointmentDetailPage'
import { AppointmentsPage } from './pages/patient/AppointmentsPage'
import { BookSlotPage } from './pages/patient/BookSlotPage'
import { FindDoctorPage } from './pages/patient/FindDoctorPage'
import { SymptomFormPage } from './pages/patient/SymptomFormPage'

export default function App(): ReactNode {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route
            path="/login"
            element={
              <RedirectIfSignedIn>
                <LoginPage />
              </RedirectIfSignedIn>
            }
          />
          <Route
            path="/register"
            element={
              <RedirectIfSignedIn>
                <RegisterPage />
              </RedirectIfSignedIn>
            }
          />
          <Route path="/" element={<HomeRedirect />} />

          {/* Patients and doctors have a calendar of their own to connect; an admin does not,
              and the API refuses them, so the route is not offered. */}
          <Route element={<RequireRole allow={['patient', 'doctor']} />}>
            <Route element={<PortalShell />}>
              <Route path="/settings/calendar" element={<CalendarSettingsPage />} />
            </Route>
          </Route>

          <Route element={<RequireRole allow={['patient']} />}>
            <Route element={<PortalShell />}>
              <Route path="/appointments" element={<AppointmentsPage />} />
              <Route path="/appointments/:appointmentId" element={<AppointmentDetailPage />} />
              <Route path="/book" element={<FindDoctorPage />} />
              <Route path="/book/:doctorId" element={<BookSlotPage />} />
              <Route path="/book/hold/:appointmentId" element={<SymptomFormPage />} />
            </Route>
          </Route>

          <Route element={<RequireRole allow={['doctor']} />}>
            <Route element={<PortalShell />}>
              <Route path="/doctor/schedule" element={<SchedulePage />} />
              <Route path="/doctor/appointments/:appointmentId" element={<VisitPage />} />
            </Route>
          </Route>

          <Route element={<RequireRole allow={['admin']} />}>
            <Route element={<PortalShell />}>
              <Route path="/admin/doctors" element={<DoctorsPage />} />
              <Route path="/admin/doctors/new" element={<NewDoctorPage />} />
              <Route path="/admin/doctors/:doctorId" element={<DoctorDetailPage />} />
              <Route path="/admin/notifications" element={<NotificationsPage />} />
              <Route path="/admin/calendar" element={<CalendarSyncPage />} />
            </Route>
          </Route>

          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  )
}
