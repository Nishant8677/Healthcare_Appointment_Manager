/** One appointment, found in the list the API already scopes to this user.
 *
 * There is no `GET /appointments/{id}` endpoint, and this is not a workaround for a missing
 * one so much as a consequence of how the list is scoped: it returns a patient's own bookings
 * or a doctor's own schedule, decided in the SQL. Filtering the result client-side therefore
 * cannot widen access — an appointment belonging to someone else was never in the response.
 *
 * The `includeCancelled` flag carries a second meaning worth knowing. With it `false`, the
 * API drops holds whose TTL has passed, so an appointment *disappearing* from the list is how
 * the symptom form learns its hold lapsed.
 */

import type { Appointment } from '../api/types'
import { appointments as api } from '../api/endpoints'
import { useResource, type Resource } from './useResource'

export function useAppointment(
  token: string,
  appointmentId: string,
  includeCancelled = true,
): Resource<Appointment | null> {
  return useResource(async () => {
    const all = await api.list(token, includeCancelled)
    return all.find((item) => item.id === appointmentId) ?? null
  }, [token, appointmentId, includeCancelled])
}
