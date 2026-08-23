document.addEventListener('DOMContentLoaded', function () {
    console.log("Admin script loaded!"); // Log when the script is loaded

    const geneDropdown = document.getElementById('id_gene'); // Gene dropdown
    const antibodyDropdown = document.getElementById('id_antibody'); // Antibody dropdown

    if (geneDropdown) {
        console.log("Gene dropdown found."); // Confirm the dropdown is found in the DOM

        // Function to handle fetching antibodies
        function updateAntibodyDropdown() {
            console.log("Lost focus on gene dropdown."); // Log when the blur event triggers
            const geneId = geneDropdown.value; // Get selected gene ID
            console.log("Gene selected:", geneId); // Log the selected gene ID

            // Clear the antibody dropdown
            antibodyDropdown.innerHTML = '<option value="">---------</option>'; // Default option

            if (geneId) {
                console.log("Fetching antibodies for gene ID:", geneId); // Log before the fetch request
                fetch(`/admin/core/experiment/antibodies_by_gene/?gene_id=${geneId}`)
                    .then(response => {
                        console.log("Fetch response received."); // Log when a response is received
                        if (!response.ok) {
                            throw new Error(`HTTP error! status: ${response.status}`);
                        }
                        return response.json();
                    })
                    .then(data => {
                        console.log("Antibodies data:", data); // Log the data returned from the server
                        if (data.antibodies && data.antibodies.length > 0) {
                            data.antibodies.forEach(antibody => {
                                const option = document.createElement('option');
                                option.value = antibody.id;
                                option.textContent = antibody.name;
                                antibodyDropdown.appendChild(option);
                            });
                            console.log("Antibodies populated in the dropdown."); // Confirm the dropdown is updated
                        } else {
                            console.log("No antibodies found for Gene ID:", geneId); // Log if no antibodies are returned
                        }
                    })
                    .catch(error => {
                        console.error("Fetch error:", error); // Log any errors during the fetch request
                    });
            } else {
                console.log("No gene selected."); // Log if no gene ID is provided
            }
        }

        // Attach 'blur' (lost focus) event listener to the gene dropdown
        geneDropdown.addEventListener('blur', updateAntibodyDropdown);
    } else {
        console.error("Gene dropdown (id_gene) not found in the DOM."); // Log if the dropdown is not found
    }
});
