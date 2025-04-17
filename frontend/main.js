"use strict";

window.addEventListener("load", () => {
  // Handle Create Link form submission
  const createLinkForm = document.getElementById("create-link-form");
  createLinkForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const site = document.getElementById("create-link-site").value.trim();
    const resultElement = document.getElementById("create-link-result");

    try {
      const response = await fetch(`/create_link?site=${encodeURIComponent(site)}`, {
        method: "POST"
      });
      if (!response.ok) {
        throw new Error(`Error creating link token: ${response.statusText}`);
      }
      const data = await response.json();
      resultElement.textContent = `Link Token: ${data.link_token}`;
    } catch (error) {
      resultElement.textContent = `Failed to create link token: ${error.message}`;
    }
  });

  // Handle Submit Credentials form
  const submitCredentialsForm = document.getElementById("submit-credentials-form");
  submitCredentialsForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const linkToken = document.getElementById("submit-link-token").value.trim();
    const username = document.getElementById("submit-username").value.trim();
    const password = document.getElementById("submit-password").value.trim();
    const resultElement = document.getElementById("submit-credentials-result");

    try {
      const response = await fetch(`/submit_credentials?link_token=${encodeURIComponent(linkToken)}&username=${encodeURIComponent(username)}&password=${encodeURIComponent(password)}`, {
        method: "POST"
      });
      if (!response.ok) {
        throw new Error(`Error submitting credentials: ${response.statusText}`);
      }
      const data = await response.json();
      resultElement.textContent = `Access Token: ${data.access_token}`;
    } catch (error) {
      resultElement.textContent = `Failed to submit credentials: ${error.message}`;
    }
  });

  // Handle Fetch Data form
  const fetchDataForm = document.getElementById("fetch-data-form");
  fetchDataForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const accessToken = document.getElementById("fetch-access-token").value.trim();
    const resultElement = document.getElementById("fetch-data-result");

    try {
      const response = await fetch(`/fetch_data?access_token=${encodeURIComponent(accessToken)}`, {
        method: "GET"
      });
      if (!response.ok) {
        throw new Error(`Error fetching data: ${response.statusText}`);
      }
      const data = await response.json();
      resultElement.textContent = JSON.stringify(data, null, 2);
    } catch (error) {
      resultElement.textContent = `Failed to fetch data: ${error.message}`;
    }
  });
});