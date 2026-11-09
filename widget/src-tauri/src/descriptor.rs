//! Is a pipe one Rafiq made? On Windows (#242) the widget reaches Rafiq through `\\.\pipe\rafiq-panel-<SID>`, and
//! sends the token only when the pipe's own security descriptor is the one Rafiq makes (rafiq/winpipe.py): owned by
//! this user's SID, one allow for that SID with nothing inherited, labelled Medium or higher. Never the process ID
//! the pipe reports, which Windows reuses once the program that made it exits. Plain text in, so it runs anywhere.

/// Integrity levels in SDDL: Medium is what a user's programs run at; Low is a sandbox's.
pub const MEDIUM: u32 = 0x2000;

/// The sections of an SDDL string (O: owner, G: group, D: DACL, S: SACL), in order.
fn sections(text: &str) -> Vec<(u8, &str)> {
    let bytes = text.as_bytes();
    let starts: Vec<usize> = (0..bytes.len().saturating_sub(1))
        .filter(|&i| b"OGDS".contains(&bytes[i]) && bytes[i + 1] == b':')
        .collect();
    if starts.first() != Some(&0) {
        return Vec::new();
    }
    starts
        .iter()
        .enumerate()
        .map(|(n, &i)| (bytes[i], &text[i + 2..starts.get(n + 1).copied().unwrap_or(text.len())]))
        .collect()
}

fn section<'a>(parts: &[(u8, &'a str)], key: u8) -> Option<&'a str> {
    parts.iter().find(|(k, _)| *k == key).map(|(_, v)| *v)
}

/// A DACL or SACL: its flags (P, AI...) and its entries, or None when it isn't one.
fn entries(value: &str) -> Option<(&str, Vec<&str>)> {
    let flags_end = value.find(|c: char| !c.is_ascii_uppercase()).unwrap_or(value.len());
    let (flags, mut rest) = value.split_at(flags_end);
    let mut found = Vec::new();
    while !rest.is_empty() {
        let inner = rest.strip_prefix('(')?;
        let end = inner.find(')')?;
        if inner[..end].contains('(') {
            return None;
        }
        found.push(&inner[..end]);
        rest = &inner[end + 1..];
    }
    Some((flags, found))
}

fn level(trustee: &str) -> u32 {
    match trustee {
        "ME" => 0x2000,
        "MP" => 0x2100,
        "HI" => 0x3000,
        "SI" => 0x4000,
        _ => trustee.strip_prefix("S-1-16-").and_then(|n| n.parse().ok()).unwrap_or(0),
    }
}

/// Rafiq's own pipe, as Windows describes it (account abbreviations already expanded by `expand_sids`)?
pub fn ok(text: &str, me: &str) -> bool {
    let parts = sections(text);
    if section(&parts, b'O') != Some(me) {
        return false;
    }
    let Some((flags, aces)) = section(&parts, b'D').and_then(entries) else {
        return false;
    };
    if !flags.contains('P') || aces.len() != 1 {
        return false;
    }
    let fields: Vec<&str> = aces[0].split(';').collect();
    if fields.len() != 6 || fields[0] != "A" || !fields[1].is_empty() || fields[2].is_empty() || fields[5] != me {
        return false;
    }
    let Some((_, labels)) = entries(section(&parts, b'S').unwrap_or("")) else {
        return false;
    };
    labels.iter().all(|ace| {
        let fields: Vec<&str> = ace.split(';').collect();
        !(fields.len() == 6 && fields[0] == "ML" && level(fields[5]) < MEDIUM)
    })
}

fn alias(value: &str) -> bool {
    value.len() == 2 && value.bytes().all(|b| b.is_ascii_uppercase())
}

/// SDDL with the owner's and each entry's account abbreviation (LA: this computer's Administrator, BA, WD...)
/// written as the full SID, as `full` says it; one it can't expand stays, and then matches no user's SID.
pub fn expand_sids(text: &str, full: impl Fn(&str) -> Option<String>) -> String {
    let expand = |value: &str| if alias(value) { full(value).unwrap_or_else(|| value.to_string()) } else { value.to_string() };
    let mut out = String::new();
    for (key, value) in sections(text) {
        out.push(key as char);
        out.push(':');
        match key {
            b'O' => out.push_str(&expand(value)),
            b'D' => match entries(value) {
                Some((flags, aces)) => {
                    out.push_str(flags);
                    for ace in aces {
                        let (head, trustee) = ace.rsplit_once(';').unwrap_or(("", ace));
                        out.push('(');
                        if ace.contains(';') {
                            out.push_str(head);
                            out.push(';');
                        }
                        out.push_str(&expand(trustee));
                        out.push(')');
                    }
                }
                None => out.push_str(value),
            },
            _ => out.push_str(value),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    const ME: &str = "S-1-5-21-1111-2222-3333-1001";
    const OTHER: &str = "S-1-5-21-1111-2222-3333-1002";

    #[test]
    fn rafiq_s_own_pipe_reads_as_this_user_s() {
        for text in [
            format!("O:{ME}D:P(A;;FA;;;{ME})S:(ML;;NW;;;ME)"),
            format!("O:{ME}D:P(A;;0x1f01ff;;;{ME})S:(ML;;NW;;;ME)"),
            format!("O:{ME}D:P(A;;FA;;;{ME})"),
            format!("O:{ME}D:PAI(A;;FA;;;{ME})S:AI(ML;;NW;;;HI)"),
        ] {
            assert!(ok(&text, ME), "{text}");
        }
    }

    #[test]
    fn a_pipe_not_made_as_rafiq_makes_it_is_refused() {
        for text in [
            format!("O:{OTHER}D:P(A;;FA;;;{ME})S:(ML;;NW;;;ME)"),
            format!("O:BAD:P(A;;FA;;;{ME})"),
            format!("O:{ME}D:(A;;FA;;;{ME})"),
            format!("O:{ME}D:P(A;;FA;;;{ME})(A;;FA;;;WD)"),
            format!("O:{ME}D:P(A;;FA;;;WD)"),
            format!("O:{ME}D:P(D;;FA;;;{OTHER})(A;;FA;;;{ME})"),
            format!("O:{ME}D:P(A;ID;FA;;;{ME})"),
            format!("O:{ME}D:P(A;;FA;;;{ME})S:(ML;;NW;;;LW)"),
            format!("O:{ME}D:P(A;;FA;;;{ME})S:(ML;;NW;;;S-1-16-4096)"),
            format!("O:{ME}D:NO_ACCESS_CONTROL"),
            format!("O:{ME}"),
            String::new(),
        ] {
            assert!(!ok(&text, ME), "{text}");
        }
    }

    #[test]
    fn account_abbreviations_in_a_real_descriptor_are_read_as_full_sids() {
        // From the windows-latest runner, where the user is this computer's Administrator (RID 500, written LA).
        let admin = "S-1-5-21-3289802278-1561746829-1498425070-500";
        let full = |alias: &str| match alias {
            "LA" => Some(admin.to_string()),
            "BA" => Some("S-1-5-32-544".to_string()),
            "WD" => Some("S-1-1-0".to_string()),
            _ => None,
        };
        let text = expand_sids("O:LAD:P(A;;FA;;;LA)S:AI(ML;;NW;;;ME)", full);
        assert_eq!(text, format!("O:{admin}D:P(A;;FA;;;{admin})S:AI(ML;;NW;;;ME)"));
        assert!(ok(&text, admin));
        assert!(!ok(&expand_sids("O:BAD:P(A;;FA;;;LA)", full), admin));
        assert!(!ok(&expand_sids("O:LAD:P(A;;FA;;;WD)", full), admin));
    }

    #[test]
    fn an_abbreviation_windows_can_t_expand_matches_no_one() {
        let text = expand_sids("O:XXD:P(A;;FA;;;XX)", |_| None);
        assert_eq!(text, "O:XXD:P(A;;FA;;;XX)");
        assert!(!ok(&text, ME));
    }
}
