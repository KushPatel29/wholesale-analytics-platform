(() => {
  const loginUrl = "/login";

  const buildNext = () => {
    try {
      return `${window.location.pathname}${window.location.search || ""}`;
    } catch (err) {
      return "/";
    }
  };

  const redirectToLogin = () => {
    const next = encodeURIComponent(buildNext());
    window.location = `${loginUrl}?next=${next}`;
  };

  const authFetch = async (input, init) => {
    const resp = await fetch(input, init);
    if (resp && resp.status === 401) {
      redirectToLogin();
      const error = new Error("auth_required");
      error.code = "auth_required";
      throw error;
    }
    return resp;
  };

  window.authFetch = authFetch;
  window.authRedirectToLogin = redirectToLogin;
})();
