-- Read-only. Returns the subject and received time of the newest message in the
-- INBOX of the account that owns the given address. Never launches Mail.
-- Output: "ok" | subject | yyyy-mm-dd hh:mm  (fields separated by ASCII 30),
-- or one of: not_running, absent, no_inbox, empty.
on run argv
	set target to item 1 of argv
	set sep to (ASCII character 30)
	if application "Mail" is not running then return "not_running"
	tell application "Mail"
		set acctIndex to 0
		repeat with i from 1 to (count of accounts)
			set addrs to (get email addresses of account i)
			repeat with j from 1 to count of addrs
				if ((item j of addrs) as text) is equal to target then set acctIndex to i
			end repeat
			if acctIndex is not 0 then exit repeat
		end repeat
		if acctIndex is 0 then return "absent"

		set mbx to missing value
		repeat with m in (get mailboxes of account acctIndex)
			if (name of m) is equal to "INBOX" then
				set mbx to contents of m
				exit repeat
			end if
		end repeat
		if mbx is missing value then return "no_inbox"

		set n to count of messages of mbx
		if n is 0 then return "empty"

		-- Mail usually keeps a mailbox sorted by date, but not always in the same
		-- direction. Look at up to 50 messages at whichever end is newer and keep
		-- the newest, so a huge inbox is never scanned in full.
		set firstDate to date received of message 1 of mbx
		set lastDate to date received of message n of mbx
		set windowSize to 50
		if n < windowSize then set windowSize to n
		if firstDate ≥ lastDate then
			set startIdx to 1
			set endIdx to windowSize
		else
			set startIdx to n - windowSize + 1
			set endIdx to n
		end if
		set bestIdx to startIdx
		set bestDate to date received of message startIdx of mbx
		repeat with k from startIdx to endIdx
			set d to date received of message k of mbx
			if d > bestDate then
				set bestDate to d
				set bestIdx to k
			end if
		end repeat
		set subj to subject of message bestIdx of mbx
	end tell
	if subj is missing value then set subj to ""
	set stamp to my fmt(bestDate)
	return "ok" & sep & subj & sep & stamp
end run

on pad(x)
	set s to x as text
	if (count of s) < 2 then set s to "0" & s
	return s
end pad

on fmt(d)
	return ((year of d) as text) & "-" & pad((month of d) as integer) & "-" & pad(day of d) & " " & pad(hours of d) & ":" & pad(minutes of d)
end fmt
