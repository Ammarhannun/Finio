// Stop a restored page or another tab's account change showing old figures.
export function mountSessionPrivacy({ userId, subscribe, target, invalidate, restore }) {
  let ended = false;
  const end = () => {
    if (ended) return;
    ended = true;
    invalidate();
  };
  const subscription = subscribe((event, session) => {
    if (ended) return;
    if (event === 'SIGNED_OUT' || (session?.user?.id && session.user.id !== userId)) end();
  });
  const onRestore = event => {
    if (!ended && event.persisted) restore();
  };
  target.addEventListener('pageshow', onRestore);
  return () => {
    ended = true;
    target.removeEventListener('pageshow', onRestore);
    subscription?.data?.subscription?.unsubscribe();
  };
}
