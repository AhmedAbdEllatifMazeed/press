import dayjs from 'dayjs/esm';
import relativeTime from 'dayjs/esm/plugin/relativeTime';
import localizedFormat from 'dayjs/esm/plugin/localizedFormat';
import updateLocale from 'dayjs/esm/plugin/updateLocale';
import isToday from 'dayjs/esm/plugin/isToday';
import duration from 'dayjs/esm/plugin/duration';
import utc from 'dayjs/esm/plugin/utc';
import timezone from 'dayjs/esm/plugin/timezone';
import advancedFormat from 'dayjs/plugin/advancedFormat';

dayjs.extend(updateLocale);
dayjs.extend(relativeTime);
dayjs.extend(localizedFormat);
dayjs.extend(isToday);
dayjs.extend(duration);
dayjs.extend(utc);
dayjs.extend(timezone);
dayjs.extend(advancedFormat);

export function dayjsLocal(dateTimeString) {
	const localTimezone = dayjs.tz.guess();
	// Server timestamps are UTC; convert to the user's local timezone
	const base = dateTimeString ? dayjs.utc(dateTimeString) : dayjs.utc();
	return base.tz(localTimezone);
}

export function dayjsIST(dateTimeString) {
	return dayjs(dateTimeString).tz('Asia/Calcutta');
}

export default dayjs;
